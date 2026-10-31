"""HTTP layer of the web UI: static files + a small JSON API over
controller.Controller, the live-audio stream, recording downloads (with
Range, so the browser's <audio> can seek) and the worker protocol (see
worker.py). Python's standard library only.

Auth: an optional shared password ("access token", Settings → Access). Off
by default - anyone who can reach the server can use it. When set, the
browser gets it once (login form, or ``#token=`` in the URL main.py opens)
and keeps it in an HttpOnly, SameSite=Strict cookie; workers send it as a
Bearer token.
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import queue
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from .. import autostart, digital_voice, i18n, military_presets
from ..config import AppConfig
from ..controller import Controller, channel_dict, channel_from_dict
from ..military_presets import BandPreset
from ..models import BlacklistEntry

STATIC_DIR = Path(__file__).resolve().parent / "static"
_COOKIE = "sst"
_MAX_JSON = 32 * 1024 * 1024
_KEEPALIVE_S = 5.0
_LOGIN_FAIL_DELAY_S = 1.0  # a threaded server: this slows a password guesser, not other users
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_KEEPALIVE = bytes(250)  # 10 ms of silence at 12.5 kHz/16-bit: keeps idle streams alive, detects dead ones


class App:
    """What the handler serves. ``mode``: "local" (this computer scans),
    "server" (same, reachable over the network) or "client" (this computer
    only works for another server - no controller, just a status page;
    ``worker`` is that worker). ``on_switch`` re-execs main.py after the
    mode changed (connect / disconnect)."""

    def __init__(self, config: AppConfig, controller: Optional[Controller], token: str, mode: str,
                 on_switch: Optional[Callable[[], None]] = None, worker: Any = None,
                 on_quit: Optional[Callable[[], None]] = None) -> None:
        self.config = config
        self.controller = controller
        self.token = token
        self.mode = mode
        self.on_switch = on_switch
        self.worker = worker
        self.on_quit = on_quit  # "Quit" in the UI - local/client mode only (a server is stopped by systemd)


def _qs(query: dict, name: str, default: Any = None) -> Any:
    values = query.get(name)
    return values[0] if values else default


def list_dirs(path: str) -> dict:
    """Folder browser for the recordings-folder picker: the browser can't
    pick a folder on the scanning machine itself (server mode, phones). A
    not-yet-created path opens at its nearest existing parent."""
    current = Path(path).expanduser() if path.strip() else Path.home()
    while not current.is_dir() and current.parent != current:
        current = current.parent
    if not current.is_dir():
        current = Path.home()
    current = current.resolve()
    try:
        names = sorted((e.name for e in os.scandir(current) if e.is_dir() and not e.name.startswith((".", "$"))), key=str.lower)
        dirs = [{"name": n, "path": str(current / n)} for n in names]
    except OSError:
        dirs = []
    listdrives = getattr(os, "listdrives", None)  # Windows, Python 3.12+
    return {
        "path": str(current),
        "parent": str(current.parent) if current.parent != current else None,
        "dirs": dirs,
        "roots": listdrives() if listdrives else ["/"],
    }


def _float_or_none(value: Optional[str]) -> Optional[float]:
    return float(value) if value not in (None, "") else None


def make_handler(app: App):
    ctl = app.controller
    lib = ctl.library if ctl is not None else None
    cfg = app.config

    class Handler(BaseHTTPRequestHandler):
        server_version = "FalconSDR/2"

        # -- plumbing -----------------------------------------------------
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _token_ok(self) -> bool:
            if not app.token:
                return True
            supplied = ""
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[7:]
            else:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                if _COOKIE in cookie:
                    supplied = unquote(cookie[_COOKIE].value)
            return hmac.compare_digest(supplied.encode(), app.token.encode())

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, value: Any, status: int = 200, extra: Optional[dict] = None) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > _MAX_JSON:
                raise ValueError("Request too large")
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw or b"{}")

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            path, query = url.path, parse_qs(url.query)
            try:
                if (app.mode == "local" and not app.token
                        and self.headers.get("Host", "").rsplit(":", 1)[0] not in _LOOPBACK_HOSTS):
                    return self._error(403, "Forbidden")  # localhost-only UI: refuse DNS-rebinding hosts
                if method in ("POST", "PUT") and self.headers.get("X-FalconSDR") != "1":
                    # A custom header forces a CORS preflight this server never
                    # answers, so other websites can't trigger actions (CSRF).
                    return self._error(403, "Forbidden")
                if not path.startswith("/api/"):
                    return self._static(path)
                if path in ("/api/session", "/api/login", "/api/logout", "/api/i18n"):
                    return self._public(method, path, query)
                if not self._token_ok():
                    return self._error(401, "Unauthorized")
                if app.mode == "client" and not path.startswith("/api/client/"):
                    return self._error(404, "Not found")
                handler = ROUTES.get((method, path))
                if handler is None:
                    return self._error(404, "Not found")
                handler(self, query)
            except FileNotFoundError:
                self._error(404, "Not found")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self._error(400, str(exc))
            except RuntimeError as exc:
                self._error(409, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._route("PUT")

        # -- static files ---------------------------------------------------
        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR not in target.parents or not target.is_file():
                target = STATIC_DIR / "index.html"  # client-side routes (#/...) all land on the app
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        # -- public endpoints -------------------------------------------------
        def _public(self, method: str, path: str, query: dict) -> None:
            if path == "/api/session":
                return self._json({
                    "authenticated": self._token_ok(), "mode": app.mode,
                    "remote": cfg.remote_receiver.url if app.mode == "client" else "",
                })
            if path == "/api/i18n":
                lang = cfg.language
                if lang == i18n.AUTO:
                    wanted = str(_qs(query, "lang", "en"))[:2].lower()
                    lang = wanted if wanted in i18n.available_languages() else "en"
                return self._json({"lang": lang, "catalog": i18n.catalog_for(lang)})
            if path == "/api/logout" and method == "POST":
                return self._json({"ok": True}, extra={"Set-Cookie": f"{_COOKIE}=; Path=/; Max-Age=0"})
            if path == "/api/login" and method == "POST":
                token = str(self._body().get("token", ""))
                if app.token and not hmac.compare_digest(token.encode(), app.token.encode()):
                    time.sleep(_LOGIN_FAIL_DELAY_S)
                    return self._error(401, "Wrong password")
                return self._json({"ok": True}, extra={
                    "Set-Cookie": f"{_COOKIE}={quote(token, safe='')}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict",
                })
            self._error(404, "Not found")

        # -- live -----------------------------------------------------------
        def live(self, query: dict) -> None:
            self._json(ctl.live(int(_qs(query, "since_log", 0) or 0)))

        def audio(self, _query: dict) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Sample-Rate", str(ctl.live()["audio_rate"]))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            q = ctl.subscribe_audio()
            try:
                while True:
                    try:
                        chunks = [q.get(timeout=_KEEPALIVE_S)]
                    except queue.Empty:
                        chunks = [_KEEPALIVE]
                    while True:
                        try:
                            chunks.append(q.get_nowait())
                        except queue.Empty:
                            break
                    self.wfile.write(b"".join(chunks))
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                ctl.unsubscribe_audio(q)

        def scanner(self, query: dict) -> None:
            action = self.path.split("?")[0].rsplit("/", 1)[-1]
            body = self._body()
            if action == "start":
                ctl.start()
            elif action == "stop":
                ctl.stop()
            else:
                ctl.command(action, body)
            self._json(ctl.live())

        # -- settings -------------------------------------------------------
        def settings(self, _query: dict) -> None:
            data = ctl.settings()
            data["curated"] = {
                "military": [channel_dict(c) for c in military_presets.MILITARY_CHANNELS],
                "fire": [channel_dict(c) for c in military_presets.FIRE_CHANNELS],
            }
            data["mode"] = app.mode
            data["config"].pop("access_token", None)
            data["password_set"] = bool(app.token)
            self._json(data)

        def config(self, _query: dict) -> None:
            ctl.patch_config(self._body())
            self._json({"ok": True})

        def channels(self, _query: dict) -> None:
            ctl.set_channels([channel_from_dict(d) for d in self._body()])
            self._json({"ok": True})

        def blacklist(self, _query: dict) -> None:
            ctl.set_blacklist([
                BlacklistEntry(freq_hz=int(d["freq_hz"]), margin_hz=int(d.get("margin_hz", 5000)),
                               label=str(d.get("label", "")), date_added=str(d.get("date_added", "")))
                for d in self._body()
            ])
            self._json({"ok": True})

        def preset_save(self, _query: dict) -> None:
            d = self._body()
            preset = military_presets.upsert_preset(BandPreset.from_dict({**d, "id": d.get("id") or ""}))
            self._json(preset.to_dict())

        def preset_delete(self, _query: dict) -> None:
            military_presets.delete_preset(str(self._body()["id"]))
            self._json({"ok": True})

        def preset_import(self, _query: dict) -> None:
            self._json({"count": military_presets.import_presets(self._body())})

        def disclaimer(self, _query: dict) -> None:
            body = self._body()
            ctl.mark_disclaimer_seen(str(body["kind"]), str(body["lang"]))
            self._json({"ok": True})

        def access(self, _query: dict) -> None:
            """Sets or clears (empty) the optional password; this browser stays logged in."""
            token = str(self._body().get("token", "")).strip()
            cfg.access_token = token
            cfg.save()
            app.token = token
            cookie = (f"{_COOKIE}={quote(token, safe='')}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"
                      if token else f"{_COOKIE}=; Path=/; Max-Age=0")
            self._json({"ok": True}, extra={"Set-Cookie": cookie})

        def quit(self, _query: dict) -> None:
            if app.mode == "server" or app.on_quit is None:
                raise ValueError("Not available on a server")
            self._json({"ok": True})
            threading.Timer(0.3, app.on_quit).start()

        def autostart(self, _query: dict) -> None:
            # Login item of the computer the program runs on - meaningless from a server's remote UI.
            if app.mode == "server" or not autostart.supported():
                raise ValueError("Not available here")
            autostart.set_enabled(bool(self._body().get("enabled")))
            self._json({"enabled": autostart.is_enabled()})

        def digital(self, _query: dict) -> None:
            """Optional digital voice add-on (digital_voice.py) - installed on
            this machine, the one that scans."""
            action = self.path.split("?")[0].rsplit("/", 1)[-1]
            if action == "install":
                digital_voice.start_install()
            elif action == "uninstall":
                if ctl.live()["running"]:
                    raise ValueError(i18n.tr("Stop the scanner first"))
                digital_voice.uninstall()
            self._json(digital_voice.install_state())

        def reset(self, _query: dict) -> None:
            ctl.reset_to_defaults()
            self._json({"ok": True})

        def connect_client(self, _query: dict) -> None:
            """Local mode -> client of another FalconSDR server."""
            if app.mode != "local" or app.on_switch is None:
                raise ValueError("Only available in local mode")
            body = self._body()
            url = str(body.get("url", "")).strip().rstrip("/")
            token = str(body.get("token", "")).strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError(i18n.tr("Enter the server address, e.g. http://192.168.1.10:8765"))
            try:
                with urlopen(Request(url + "/api/live", headers={"Authorization": f"Bearer {token}"}), timeout=5):
                    pass
            except URLError as exc:
                raise RuntimeError(i18n.tr("Can't connect: {error}").format(error=getattr(exc, "reason", exc)))
            rr = cfg.remote_receiver
            rr.enabled, rr.url, rr.token = True, url, token
            cfg.save()
            self._json({"ok": True, "url": url, "token": token})
            threading.Timer(0.5, app.on_switch).start()

        def client_status(self, _query: dict) -> None:
            w = app.worker
            self._json({
                "url": cfg.remote_receiver.url, "token": cfg.remote_receiver.token,
                "name": w.name if w else "", "connected": bool(w and w._connected),
                "info": w._info() if w else {},
            })

        def client_disconnect(self, _query: dict) -> None:
            cfg.remote_receiver.enabled = False
            cfg.save()
            self._json({"ok": True})
            if app.on_switch:
                threading.Timer(0.5, app.on_switch).start()

        # -- library --------------------------------------------------------
        def _filters(self, query: dict) -> dict:
            return {
                "sel": [s for s in query.get("sel", []) if s],
                "q": str(_qs(query, "q", "")),
                "since": _float_or_none(_qs(query, "since")),
                "until": _float_or_none(_qs(query, "until")),
                "min_quality": float(_qs(query, "minq", 0) or 0),
            }

        def library(self, query: dict) -> None:
            self._json({
                **lib.query(**self._filters(query), sort=str(_qs(query, "sort", "new")),
                            offset=int(_qs(query, "offset", 0)), limit=min(int(_qs(query, "limit", 100)), 500)),
                "pending": lib.pending_count(),
            })

        def library_ids(self, query: dict) -> None:
            self._json(lib.ids(**self._filters(query)))

        def facets(self, _query: dict) -> None:
            self._json(lib.facets())

        def stats(self, query: dict) -> None:
            self._json(lib.stats(sel=query.get("sel", []), utc=_qs(query, "utc") == "1"))

        def dirs(self, query: dict) -> None:
            self._json(list_dirs(str(_qs(query, "path", ""))))

        def storage(self, _query: dict) -> None:
            victims = lib.retention_victims()
            sizes = sum(lib.get(rel).size for rel in victims if lib.get(rel))
            self._json({**lib.storage(), "retention_pending": len(victims), "retention_bytes": sizes})

        def lib_delete(self, _query: dict) -> None:
            self._json({"deleted": lib.delete(self._body()["ids"])})

        def lib_retention_now(self, _query: dict) -> None:
            self._json({"deleted": lib.apply_retention()})

        def lib_favorite(self, _query: dict) -> None:
            body = self._body()
            lib.set_favorite(body["ids"], bool(body.get("favorite", True)))
            self._json({"ok": True})

        def lib_transcript(self, _query: dict) -> None:
            body = self._body()
            lib.set_transcript_text(str(body["id"]), str(body.get("text", "")))
            self._json({"ok": True})

        def lib_transcribe(self, _query: dict) -> None:
            body = self._body()
            lib.request_transcription(body["ids"], body.get("language") or None)
            self._json({"ok": True})

        def recording(self, query: dict) -> None:
            target = lib.path_of(str(_qs(query, "id", "")))
            size = target.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            status = 200
            if rng.startswith("bytes="):
                first, _, last = rng[6:].split(",")[0].partition("-")
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:
                    start = max(0, size - int(last))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if _qs(query, "download"):
                self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
            self.end_headers()
            if self.command == "HEAD":
                return
            with target.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def peaks(self, query: dict) -> None:
            self._json(lib.peaks(str(_qs(query, "id", ""))))

        # -- computers (workers) ------------------------------------------------
        def workers(self, _query: dict) -> None:
            self._json({"workers": ctl.workers_view(), "pending": lib.pending_count()})

        def workers_update(self, _query: dict) -> None:
            body = self._body()
            ctl.update_worker(str(body["name"]), body.get("settings", {}))
            self._json({"ok": True})

        def workers_command(self, _query: dict) -> None:
            body = self._body()
            ctl.send_worker_command(str(body["name"]), str(body["command"]))
            self._json({"ok": True})

        def workers_forget(self, _query: dict) -> None:
            ctl.forget_worker(str(self._body()["name"]))
            self._json({"ok": True})

        # -- worker protocol (see worker.py) -------------------------------------
        def w_heartbeat(self, _query: dict) -> None:
            body = self._body()
            self._json(ctl.heartbeat(str(body["name"]), body.get("info", {})))

        def w_claim(self, _query: dict) -> None:
            self._json({"job": lib.claim_job(str(self._body()["name"]))})

        def w_result(self, _query: dict) -> None:
            body = self._body()
            self._json({"ok": lib.submit_result(str(body["id"]), str(body["key"]), body["result"])})

        def w_release(self, _query: dict) -> None:
            lib.release_job(str(self._body()["id"]))
            self._json({"ok": True})

        def w_files(self, _query: dict) -> None:
            self._json(lib.files())

        def w_mirrored(self, _query: dict) -> None:
            lib.mark_mirrored(self._body()["ids"])
            self._json({"ok": True})

        def w_import(self, _query: dict) -> None:
            self._json({"imported": lib.import_metadata(self._body()["items"])})

        def w_upload(self, query: dict) -> None:
            rel = str(_qs(query, "id", ""))
            target = lib.upload_target(rel)
            length = int(self.headers.get("Content-Length") or -1)
            if length < 0:
                raise ValueError("Content-Length required")
            if target.exists() and target.stat().st_size == length:
                self.rfile.read(length)
                return self._json({"ok": True, "skipped": True})
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            with partial.open("wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise ValueError("Upload interrupted")
                    f.write(chunk)
                    remaining -= len(chunk)
            mtime = float(self.headers.get("X-Mtime") or 0)
            if mtime:
                os.utime(partial, (mtime, mtime))
            partial.replace(target)
            lib.add_file(target)
            self._json({"ok": True})

    ROUTES: dict[tuple[str, str], Callable] = {
        ("GET", "/api/live"): Handler.live,
        ("GET", "/api/audio"): Handler.audio,
        ("POST", "/api/scanner/start"): Handler.scanner,
        ("POST", "/api/scanner/stop"): Handler.scanner,
        ("POST", "/api/scanner/skip"): Handler.scanner,
        ("POST", "/api/scanner/blacklist"): Handler.scanner,
        ("POST", "/api/scanner/hold"): Handler.scanner,
        ("POST", "/api/scanner/manual_record"): Handler.scanner,
        ("POST", "/api/scanner/raw_iq"): Handler.scanner,
        ("POST", "/api/scanner/solo"): Handler.scanner,
        ("GET", "/api/settings"): Handler.settings,
        ("POST", "/api/config"): Handler.config,
        ("PUT", "/api/channels"): Handler.channels,
        ("PUT", "/api/blacklist"): Handler.blacklist,
        ("POST", "/api/presets/save"): Handler.preset_save,
        ("POST", "/api/presets/delete"): Handler.preset_delete,
        ("POST", "/api/presets/import"): Handler.preset_import,
        ("POST", "/api/disclaimer"): Handler.disclaimer,
        ("POST", "/api/reset"): Handler.reset,
        ("POST", "/api/access"): Handler.access,
        ("POST", "/api/quit"): Handler.quit,
        ("POST", "/api/autostart"): Handler.autostart,
        ("GET", "/api/digital"): Handler.digital,
        ("POST", "/api/digital/install"): Handler.digital,
        ("POST", "/api/digital/uninstall"): Handler.digital,
        ("POST", "/api/client/quit"): Handler.quit,
        ("POST", "/api/client/connect"): Handler.connect_client,
        ("GET", "/api/client/status"): Handler.client_status,
        ("POST", "/api/client/disconnect"): Handler.client_disconnect,
        ("GET", "/api/library"): Handler.library,
        ("GET", "/api/library/ids"): Handler.library_ids,
        ("GET", "/api/library/facets"): Handler.facets,
        ("GET", "/api/library/stats"): Handler.stats,
        ("GET", "/api/library/storage"): Handler.storage,
        ("GET", "/api/dirs"): Handler.dirs,
        ("POST", "/api/library/delete"): Handler.lib_delete,
        ("POST", "/api/library/retention"): Handler.lib_retention_now,
        ("POST", "/api/library/favorite"): Handler.lib_favorite,
        ("POST", "/api/library/transcript"): Handler.lib_transcript,
        ("POST", "/api/library/transcribe"): Handler.lib_transcribe,
        ("GET", "/api/recording"): Handler.recording,
        ("GET", "/api/recording/peaks"): Handler.peaks,
        ("GET", "/api/workers"): Handler.workers,
        ("POST", "/api/workers/update"): Handler.workers_update,
        ("POST", "/api/workers/command"): Handler.workers_command,
        ("POST", "/api/workers/forget"): Handler.workers_forget,
        ("POST", "/api/worker/heartbeat"): Handler.w_heartbeat,
        ("POST", "/api/worker/claim"): Handler.w_claim,
        ("POST", "/api/worker/result"): Handler.w_result,
        ("POST", "/api/worker/release"): Handler.w_release,
        ("GET", "/api/worker/files"): Handler.w_files,
        ("POST", "/api/worker/mirrored"): Handler.w_mirrored,
        ("POST", "/api/worker/import"): Handler.w_import,
        ("PUT", "/api/worker/upload"): Handler.w_upload,
    }
    return Handler


class _Server(ThreadingHTTPServer):
    # socketserver's default listen backlog is 5, but one page load opens ~20
    # connections at once (HTTP/1.0: every JS module is its own request).
    # Reported on macOS: a blank page on every other load - one module never loaded.
    request_queue_size = 128


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    httpd = _Server((host, port), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd
