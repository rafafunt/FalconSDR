"""FalconSDR entry point - starts the web UI (and opens it in the browser).

  python main.py            this computer scans: UI on http://127.0.0.1:8765
                            (or, if connected to a server, works as its helper:
                            local transcription / copies, opens the server's UI)
  python main.py --server   headless receiver reachable over the network (systemd);
                            open to everyone on the network unless a password is
                            set in Settings → Access
  python main.py --local    ignore a saved server connection, scan locally
  --start                   start scanning right away
  --no-browser              don't open the browser
  --self-test               check this install/build has everything it needs, then exit
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Transcription may live in this repo's own .venv (PEP 668 fallback - see
# transcription_install.py): re-exec into it before importing anything heavy.
if not getattr(sys, "frozen", False):
    from falconsdr.venv_paths import VENV_DIR, VENV_PYTHON
else:
    VENV_DIR = VENV_PYTHON = None
if VENV_PYTHON is not None and VENV_PYTHON.exists() and Path(sys.prefix).resolve() != VENV_DIR.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

# Packaged build: helper processes multiprocessing starts (Whisper's libraries
# use its resource tracker) re-run this executable - let them run, not the app.
if getattr(sys, "frozen", False):
    import multiprocessing
    multiprocessing.freeze_support()

# Packaged build only: `falconsdr --pip ...` runs the pip bundled inside it
# (the in-app transcription installer - transcription_install.py).
if getattr(sys, "frozen", False) and sys.argv[1:2] == ["--pip"]:
    import pip._vendor.distlib as _distlib
    from pip._vendor.distlib import resources as _resources
    from pip._internal.cli.main import main as _pip_main
    # distlib finds its script-launcher templates (t64.exe...) through the
    # module's loader, which it doesn't know for PyInstaller's; the files sit
    # next to the module on disk (--collect-all pip), so a plain file finder works.
    _resources.register_finder(_distlib.__loader__, _resources.ResourceFinder)
    sys.exit(_pip_main(sys.argv[2:]))

import argparse  # noqa: E402
import faulthandler  # noqa: E402
import logging  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import webbrowser  # noqa: E402
from urllib.parse import quote  # noqa: E402
from urllib.request import urlopen  # noqa: E402

from falconsdr import __version__  # noqa: E402
from falconsdr.config import CONFIG_DIR, AppConfig  # noqa: E402

DEFAULT_PORT = 8765


def _restart() -> None:
    """Re-exec this program with the same arguments (mode switch)."""
    args = [a for a in sys.argv[1:] if a != "--local"]
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable, *args])
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *args])


def _already_running(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/session", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="FalconSDR")
    parser.add_argument("--server", action="store_true", help="headless receiver reachable over the network")
    parser.add_argument("--local", action="store_true", help="scan locally even if connected to a server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--start", action="store_true", help="start scanning immediately")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--version", action="version", version=f"FalconSDR {__version__}")
    parser.add_argument("--self-test", action="store_true", help="check this install has everything it needs, then exit")
    args = parser.parse_args()

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        # Windowed build (no console): keep the log where support can find it.
        log = (CONFIG_DIR / "falconsdr.log").open("w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    if getattr(sys, "frozen", False):
        from falconsdr.playback.transcription_install import enable_addons
        enable_addons()
    if args.self_test:
        from falconsdr import selftest
        return selftest.run()
    faulthandler.enable(file=(CONFIG_DIR / "crash.log").open("a", encoding="utf-8", buffering=1), all_threads=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from falconsdr import i18n
    from falconsdr.web.server import App, serve
    from falconsdr.worker import Worker

    config = AppConfig.load()
    i18n.set_language(config.language)
    client_mode = config.remote_receiver.enabled and not args.server and not args.local
    host = args.host or ("0.0.0.0" if args.server else "127.0.0.1")

    if not args.server and _already_running(args.port):
        # A second launch just brings the running UI up again.
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        return 0

    token = config.access_token  # optional - empty = no login needed
    if args.server:
        print(f"FalconSDR server: http://{socket.gethostname()}:{args.port}/", flush=True)

    stop = threading.Event()
    restart = threading.Event()

    def switch_mode() -> None:  # connect / disconnect: shut down cleanly, then re-exec
        restart.set()
        stop.set()

    controller = worker = None
    if client_mode:
        rr = config.remote_receiver
        worker = Worker(rr.url, rr.token, name=os.environ.get("FALCONSDR_WORKER_NAME", ""),
                        local_root=Path(config.recording.output_dir),
                        on_detach=lambda: (setattr(rr, "enabled", False), config.save(), stop.set()),
                        on_restart=switch_mode)
        worker.start()
        app = App(config, None, token, "client", on_switch=switch_mode, worker=worker, on_quit=stop.set)
        browser_url = f"{rr.url}/#token={quote(rr.token, safe='')}"
        print(i18n.tr("Working for the server {url} - press Ctrl+C to stop.").format(url=rr.url), flush=True)
    else:
        from falconsdr.controller import Controller
        controller = Controller(config)
        # Indexing a large library takes a while - never hold up the UI or the scanner for it.
        threading.Thread(target=controller.start_background, daemon=True, name="library-start").start()
        app = App(config, controller, token, "server" if args.server else "local", on_switch=switch_mode,
                  on_quit=stop.set)
        browser_url = f"http://127.0.0.1:{args.port}/"
        # This computer's own transcription helper - talks to its own API.
        worker = Worker(f"http://127.0.0.1:{args.port}", token, name=os.environ.get("FALCONSDR_WORKER_NAME", ""),
                        embedded=True, on_restart=switch_mode)
        worker.start()
        if args.start:
            controller.start()

    try:
        httpd = serve(app, host, args.port)
    except OSError as exc:
        print(f"Can't listen on {host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    if not args.server and not args.no_browser:
        webbrowser.open(browser_url)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    # Clean shutdown closes every open recording properly (systemd stop,
    # sdr-mode switching, Ctrl+C).
    httpd.shutdown()
    if worker is not None:
        worker.stop()
    if controller is not None:
        controller.shutdown()
    if restart.is_set():
        _restart()
    return 0


if __name__ == "__main__":
    sys.exit(main())
