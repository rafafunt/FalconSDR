"""Installing an external add-on from the author's own source, on the user's computer.

Nothing of the program is bundled or redistributed by FalconSDR: it downloads the author's pinned release
source (checked against a SHA-256, so a re-pointed tag fails the install instead of running other code),
builds it here and keeps the result in FalconSDR's own folder. The licence of each program stays the author's
(dumpvdl2 GPL-3.0, libacars MIT); building on the user's own machine is ordinary use of GPL software. The app
never asks for a password: missing build packages are reported with the one command to run (same as dsd-fme,
AGENTS.md gotcha 45)."""
from __future__ import annotations

import gzip
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

from ..audio_transcode import NO_WINDOW, system_env
from ..digital_voice import _urlopen     # the certificate fallbacks of a packaged build
from ..i18n import tr
from .external import BIN_DIR, tool_path, which

# Pinned releases and the SHA-256 of the *gunzipped* tar (GitHub may re-compress a tag's tarball). Update together.
_VDL2_SOURCES = (
    ("libacars", "https://codeload.github.com/szpajder/libacars/tar.gz/refs/tags/v2.2.1",
     "ac1777b8655e25f55ddd50c41e4710c8fce1b1a9c0a286aa55f5261af2c78c78"),
    ("dumpvdl2", "https://codeload.github.com/szpajder/dumpvdl2/tar.gz/refs/tags/v2.7.0",
     "3f5f0648da4d1ab310196ffb143cade75518d5c36669469500ede23a22dfad29"),
)
_BUILD_PACKAGES = {"glib-2.0": "libglib2.0-dev", "librtlsdr": "librtlsdr-dev"}

# rtl_433 publishes ready builds for Windows and macOS (its own releases page): downloaded as they are, checked
# against the SHA-256 GitHub shows for each asset. Linux has the distribution's package (apt install rtl-433).
_R433 = "https://github.com/merbanan/rtl_433/releases/download"
_RTL433_ASSETS = {
    ("win32", "amd64"): (f"{_R433}/25.12/rtl_433-win-x64-25.12.zip", "ed42651d2a2a94f6419f7675955bd65449d8247e27822c65c363595b715b7de6"),
    ("darwin", "arm64"): (f"{_R433}/25.12/rtl_433-rtlsdr-MacOS-arm64-25.12.zip", "62d5f7d4963ad2feb93ad40a927c1804f59269a09ee7850c9e536f7e167a9256"),
    ("darwin", "x86_64"): (f"{_R433}/25.02/rtl_433-rtlsdr-MacOS-x86_64-25.02.zip", "9bd2dab719c7299e0efd4e73b5594465ac5519f399142b43263b3c5fb151a3e7"),
}


def _rtl433_asset() -> tuple[str, str] | None:
    return _RTL433_ASSETS.get((sys.platform, platform.machine().lower()))

_lock = threading.Lock()
_states: dict[str, dict] = {}


def can_install(addon_id: str) -> bool:
    """vdl2: built from source (Linux, macOS); rtl_433: the author's ready build (Windows, macOS);
    tetra: built locally from tetra-rs/ via cargo."""
    if addon_id == "tetra":
        from . import tetra
        return tetra.can_build_tetra()
    if addon_id == "tetra_codec":
        from . import tetra_codec
        return tetra_codec.can_install()
    if addon_id == "vdl2":
        return sys.platform != "win32"
    return addon_id == "rtl_433" and _rtl433_asset() is not None


def install_dir(addon_id: str) -> Path:
    return BIN_DIR / addon_id


def state(addon_id: str) -> dict:
    if addon_id == "tetra_codec":
        from . import tetra_codec
        return tetra_codec.install_state()
    if addon_id == "tetra":
        from . import tetra
        return tetra.install_state()
    with _lock:
        s = _states.get(addon_id) or {"running": False, "log": [], "error": "", "hint": ""}
        return {**s, "log": list(s["log"][-12:])}


def start(addon_id: str) -> None:
    if addon_id == "tetra_codec":
        from . import tetra_codec
        tetra_codec.start()
        return
    if addon_id == "tetra":
        from . import tetra
        tetra.start_build()
        return
    if not can_install(addon_id):
        raise RuntimeError(tr("FalconSDR cannot install this one on this computer: put the program in {folder} or set its path below.").format(folder=install_dir(addon_id)))
    with _lock:
        if _states.get(addon_id, {}).get("running"):
            return
        _states[addon_id] = {"running": True, "log": [], "error": "", "hint": ""}
    threading.Thread(target=_install_vdl2 if addon_id == "vdl2" else _install_rtl433, daemon=True, name="addon-install").start()


def uninstall(addon_id: str) -> None:
    if addon_id == "tetra_codec":
        from . import tetra_codec
        tetra_codec.uninstall()
        return
    if addon_id == "tetra":
        from . import tetra
        tetra.uninstall_tetra()
        return
    with _lock:
        if _states.get(addon_id, {}).get("running"):
            raise RuntimeError(tr("Installation is in progress - wait for it to finish."))
        _states.pop(addon_id, None)
    shutil.rmtree(install_dir(addon_id), ignore_errors=True)


def _say(addon_id: str, line: str) -> None:
    with _lock:
        st = _states.get(addon_id)
        if st is not None:
            st["log"].append(line)


def install_hint() -> str:
    """The command that installs what building needs here (root: the user runs it)."""
    if sys.platform == "darwin":
        return "brew install cmake pkg-config glib librtlsdr"
    ids = ""
    try:
        ids = " ".join(re.findall(r'^(?:ID|ID_LIKE)="?([^"\n]*)', Path("/etc/os-release").read_text(encoding="utf-8"), re.M))
    except OSError:
        pass
    if re.search(r"\b(fedora|rhel|centos)\b", ids):
        return "sudo dnf install gcc gcc-c++ make cmake pkgconf-pkg-config glib2-devel rtl-sdr-devel zlib-devel libxml2-devel"
    if re.search(r"\barch\b", ids):
        return "sudo pacman -S --needed base-devel cmake glib2 rtl-sdr libxml2"
    return "sudo apt install build-essential cmake pkg-config libglib2.0-dev librtlsdr-dev libusb-1.0-0-dev zlib1g-dev libxml2-dev"


def _download(addon_id: str, name: str, url: str, sha256: str, dest: Path) -> None:
    _say(addon_id, f"Downloading {name}…")
    with _urlopen(url) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)
    digest = hashlib.sha256()
    with gzip.open(dest, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != sha256:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{name}: the download is not the tested version (checksum mismatch) - not installed")


def _run(addon_id: str, cmd: list[str], env: dict) -> None:
    # A build must not starve a scanner that is running: lowest priority on POSIX (via `nice`, never
    # preexec_fn which can deadlock after fork() in a multithreaded process).
    run_cmd = ["nice", "-n", "15", *cmd] if os.name == "posix" and shutil.which("nice") else cmd
    proc = subprocess.run(run_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
    if proc.returncode != 0:
        for line in proc.stdout.decode("utf-8", "replace").strip().splitlines()[-6:]:
            _say(addon_id, line)
        raise RuntimeError(f"{Path(cmd[0]).name} failed")


def _install_vdl2() -> None:
    addon_id = "vdl2"
    try:
        _say(addon_id, "dumpvdl2 (GPL-3.0) and libacars (MIT) are built here from the authors' source code.")
        missing = [tool for tool in ("cmake", "make", "cc", "pkg-config") if not which(tool)]
        env = dict(system_env() or os.environ)
        env["PATH"] = tool_path()
        prefix = install_dir(addon_id) / "prefix"
        for var in ("PKG_CONFIG_PATH",):
            env[var] = os.pathsep.join(filter(None, [str(prefix / "lib" / "pkgconfig"), str(prefix / "lib64" / "pkgconfig"), env.get(var, "")]))
        if not missing:
            for module in _BUILD_PACKAGES:
                if subprocess.run(["pkg-config", "--exists", module], env=env).returncode != 0:
                    missing.append(_BUILD_PACKAGES[module])
        if missing:
            with _lock:
                _states[addon_id]["hint"] = install_hint()
            raise RuntimeError("Missing for the build: " + ", ".join(missing))
        work = install_dir(addon_id).with_name("vdl2-build")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        try:
            jobs = str(max(1, (os.cpu_count() or 2) // 2))      # half the cores: the scanner may be running
            for name, url, sha256 in _VDL2_SOURCES:
                archive = work / f"{name}.tar.gz"
                _download(addon_id, name, url, sha256, archive)
                with tarfile.open(archive) as tar:
                    top = tar.getnames()[0].split("/")[0]
                    if hasattr(tarfile, "data_filter"):       # Python 3.11.4+
                        tar.extractall(work, filter="data")
                    else:
                        tar.extractall(work)
                src = work / top
                _say(addon_id, f"Building {name} (a few minutes)…")
                _run(addon_id, ["cmake", "-S", str(src), "-B", str(src / "build"), "-DCMAKE_BUILD_TYPE=Release",
                                f"-DCMAKE_INSTALL_PREFIX={prefix}", "-DCMAKE_INSTALL_LIBDIR=lib",
                                f"-DCMAKE_PREFIX_PATH={prefix}", f"-DCMAKE_INSTALL_RPATH={prefix / 'lib'}",
                                "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"], env)
                _run(addon_id, ["cmake", "--build", str(src / "build"), "-j", jobs], env)
                _run(addon_id, ["cmake", "--install", str(src / "build")], env)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not (prefix / "bin" / "dumpvdl2").is_file():
            raise RuntimeError("dumpvdl2 was built but not found")
        _say(addon_id, "Done.")
    except Exception as exc:    # shown on the Add-ons page: the only place the user sees why
        with _lock:
            _states[addon_id]["error"] = str(exc)
    finally:
        with _lock:
            _states[addon_id]["running"] = False


def _install_rtl433() -> None:
    addon_id = "rtl_433"
    try:
        url, sha256 = _rtl433_asset()          # can_install() was true
        _say(addon_id, "rtl_433 (GPL-2.0) is downloaded as the author published it.")
        folder = install_dir(addon_id)
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / "download.zip"
        _say(addon_id, "Downloading rtl_433…")
        with _urlopen(url) as resp, open(archive, "wb") as out:
            shutil.copyfileobj(resp, out)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != sha256:
            archive.unlink(missing_ok=True)
            raise RuntimeError("rtl_433: the download is not the tested version (checksum mismatch) - not installed")
        _say(addon_id, "Unpacking…")
        with zipfile.ZipFile(archive) as z:
            z.extractall(folder)
        archive.unlink()
        exe = folder / ("rtl_433.exe" if sys.platform == "win32" else "rtl_433")
        if not exe.is_file():
            raise RuntimeError("rtl_433 was unpacked but not found")
        if sys.platform != "win32":
            exe.chmod(0o755)                    # a zip does not keep the executable bit
        # The macOS build links Homebrew's librtlsdr and libusb, the Windows one needs the VC++ runtime: say so now, not at the first start.
        kwargs = {"creationflags": NO_WINDOW} if sys.platform == "win32" else {}
        try:
            check = subprocess.run([str(exe), "-V"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                                   env=system_env(), stdin=subprocess.DEVNULL, **kwargs)
            ok = "rtl_433" in (check.stdout + check.stderr)
            reason = (check.stderr or check.stdout).strip().splitlines()[-1:] or [""]
        except (OSError, subprocess.SubprocessError) as exc:
            ok, reason = False, [str(exc)]
        if not ok:
            # Don't leave an unusable binary behind - otherwise find_program() reports it as installed.
            shutil.rmtree(folder, ignore_errors=True)
            if sys.platform == "darwin":
                with _lock:
                    _states[addon_id]["hint"] = "brew install librtlsdr libusb openssl@3"
            raise RuntimeError("rtl_433 was installed but does not start: " + reason[0][:200])
        _say(addon_id, "Done.")
    except Exception as exc:
        with _lock:
            _states[addon_id]["error"] = str(exc)
            if sys.platform == "darwin" and not _states[addon_id].get("hint"):
                _states[addon_id]["hint"] = "brew install rtl_433"
    finally:
        with _lock:
            _states[addon_id]["running"] = False
