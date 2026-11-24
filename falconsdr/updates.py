"""Update check and, where it can be done safely, a one-click update (AGENTS.md gotcha 61).

Asks GitHub for the latest release - only when the user said yes (``config.updates.check``; None = not asked
yet, the UI asks once). The request carries nothing but a generic User-Agent. What the UI offers depends on how
this copy was installed (``install_kind``): the Windows installer and an AppImage update themselves; a .deb/.rpm
needs root, so it gets the download and the command; everything else the download or the release page.
A one-click update installs only a file whose SHA-256 matches the release's own SHA256SUMS.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen

from . import __version__

REPO = "rafafunt/FalconSDR"
SERVER_INSTALL = f"curl -fsSL https://raw.githubusercontent.com/{REPO}/master/packaging/install.sh | bash -s -- --server"
_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
_CHECK_EVERY_S = 24 * 3600
_RETRY_AFTER_ERROR_S = 3600
_TIMEOUT_S = 15
_NOTES_MAX = 4000


def parse_version(text: str) -> tuple[int, ...]:
    """"v1.7.0" -> (1, 7, 0); anything unparsable -> () (never "newer")."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(x) for x in m[1].split(".")) if m else ()


def _request(url: str) -> Request:
    return Request(url, headers={"User-Agent": f"FalconSDR/{__version__}", "Accept": "application/vnd.github+json"})


def release_notes(body: str) -> dict:
    """The "What's new" list of a release body as plain text, per language: {"en": ..., "pl": ...}. The release page is
    Markdown with a download banner and the Polish text inside <details>; the app shows text, not Markdown."""
    out = {}
    for lang, heading in (("en", r"###\s+What's new"), ("pl", r"####\s+Nowości")):
        # The list ends at the next heading or at the bold "Server install" paragraph that follows it.
        m = re.search(rf"^{heading}\s*$(.*?)(?=^\s*#|^\s*\*\*[^*\n]+\*\*\s+[^-\n]|\Z)", body, re.M | re.S)
        if m:
            text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", m[1])          # [text](url) -> text
            text = re.sub(r"\*\*([^*]+)\*\*|<[^>]+>", lambda g: g[1] or "", text)   # **bold** -> bold, tags dropped
            text = re.sub(r"\*([^*\s][^*\n]*)\*", r"\1", text)                  # *italic* -> italic ("- " bullets stay)
            out[lang] = text.strip()[:_NOTES_MAX]
    if not out:     # a release written another way: its text, cleaned the same way
        out["en"] = re.sub(r"\*\*|<[^>]+>", "", re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)).strip()[:_NOTES_MAX]
    return out


def fetch_latest() -> dict:
    """The latest published release (GitHub leaves out drafts and pre-releases)."""
    with urlopen(_request(_LATEST_URL), timeout=_TIMEOUT_S) as r:
        data = json.loads(r.read())
    return {
        "version": str(data.get("tag_name", "")).lstrip("v"),
        "notes": release_notes(str(data.get("body") or "")),
        "url": str(data.get("html_url", "")),
        "published": str(data.get("published_at", "")),
        "assets": [{"name": a["name"], "url": a["browser_download_url"], "size": int(a.get("size", 0))}
                   for a in data.get("assets", []) if a.get("name") and a.get("browser_download_url")],
    }


def install_kind() -> str:
    """How this copy was installed - decides what an update can do."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if sys.platform == "win32":
            return "windows-setup" if (exe_dir / "unins000.exe").exists() else "windows-zip"   # Inno Setup's uninstaller
        if sys.platform == "darwin":
            return "macos"
        if Path("/var/lib/dpkg/info/falconsdr.list").exists():
            return "deb"
        if any(Path("/var/lib/pacman/local").glob("falconsdr-*")):
            return "aur"
        return "rpm"
    root = Path(__file__).resolve().parent.parent
    if root == Path("/opt/falconsdr") and Path("/etc/systemd/system/falconsdr-receiver.service").exists():
        return "server"     # packaging/install.sh --server: the same line updates it
    return "git" if (Path(__file__).resolve().parent.parent / ".git").exists() else "source"


def _arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)


def pick_asset(kind: str, assets: list[dict], arch: str = "") -> Optional[dict]:
    """The release file for this kind of install, None when the release has none (e.g. a Raspberry Pi)."""
    arch = arch or _arch()
    deb_arch = {"x86_64": "amd64"}.get(arch, arch)
    suffix = {
        "windows-setup": "-windows-x64-setup.exe", "windows-zip": "-windows-x64-portable.zip",
        "appimage": f"-{arch}.AppImage", "deb": f"_{deb_arch}.deb", "rpm": f".{arch}.rpm",
    }.get(kind)
    for a in assets:
        if suffix and a["name"].endswith(suffix):
            return a
        if kind == "macos" and f"-macos-{arch}" in a["name"] and a["name"].endswith(".zip"):
            return a
    return None


def action_for(kind: str, latest: dict) -> dict:
    """What the UI offers: "install" (one click), "download" (+ a command for a package manager) or "page"."""
    asset = pick_asset(kind, latest.get("assets", []))
    if kind in ("windows-setup", "appimage") and asset and can_install(kind):
        return {"type": "install", "asset": asset}
    if kind == "deb" and asset:
        return {"type": "download", "asset": asset, "command": f"sudo apt install ./{asset['name']}"}
    if kind == "rpm" and asset:
        return {"type": "download", "asset": asset, "command": f"sudo dnf install ./{asset['name']}"}
    if kind == "server":
        return {"type": "command", "command": SERVER_INSTALL}
    if kind == "aur":
        return {"type": "command", "command": "yay -Syu falconsdr"}
    if kind == "git":
        root = Path(__file__).resolve().parent.parent
        return {"type": "command", "command": f'cd "{root}" && git pull'}
    if asset:
        return {"type": "download", "asset": asset}
    return {"type": "page"}


def can_install(kind: str) -> bool:
    if kind == "appimage":
        target = Path(os.environ.get("APPIMAGE", ""))
        return target.is_file() and os.access(target.parent, os.W_OK)
    return kind == "windows-setup"


def checksum_for(sums_text: str, name: str) -> str:
    """The hash of ``name`` in a SHA256SUMS file ("<hex>  <name>" or "<hex> *<name>"), "" if missing."""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return ""


class UpdateChecker:
    """One per process: checks at start and then once a day while ``config.updates.check`` is on."""

    def __init__(self, config) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._latest: Optional[dict] = None
        self._checked_at = 0.0       # wall clock of the last successful check
        self._next = 0.0             # monotonic: when to try again
        self._error = ""
        self._install = {"state": "idle", "progress": 0, "error": ""}
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="updates").start()

    # -- checking ----------------------------------------------------------------------------------------
    def _loop(self) -> None:
        self._wake.wait(20)          # not in the busy first seconds of a start
        while True:
            self._wake.clear()
            if self._config.updates.check and time.monotonic() >= self._next:
                self.check_now()
            self._wake.wait(60)

    def wake(self) -> None:
        """The setting was just switched on: check without waiting a minute."""
        self._wake.set()

    def check_now(self) -> dict:
        try:
            latest = fetch_latest()
            with self._lock:
                self._latest, self._error = latest, ""
                self._checked_at = time.time()
                self._next = time.monotonic() + _CHECK_EVERY_S
        except (OSError, ValueError, KeyError, TypeError) as exc:
            with self._lock:
                self._error = str(getattr(exc, "reason", exc))
                self._next = time.monotonic() + _RETRY_AFTER_ERROR_S
        return self.status()

    def available(self) -> str:
        """The newer version's number, "" when there is none (for the top bar)."""
        with self._lock:
            latest = self._latest
        if not latest or not self._config.updates.check:
            return ""
        return latest["version"] if parse_version(latest["version"]) > parse_version(__version__) else ""

    def status(self) -> dict:
        kind = install_kind()
        with self._lock:
            latest, checked_at, error, install = self._latest, self._checked_at, self._error, dict(self._install)
        newer = bool(latest) and parse_version(latest["version"]) > parse_version(__version__)
        return {
            "check": self._config.updates.check, "current": __version__, "kind": kind,
            "latest": {k: v for k, v in latest.items() if k != "assets"} if latest else None,
            "available": newer, "action": action_for(kind, latest) if newer else None,
            "checked_at": checked_at or None, "error": error, "install": install,
        }

    # -- installing --------------------------------------------------------------------------------------
    def install(self, quit_app: Callable[[], None], restart_app: Callable[[], None]) -> None:
        """Downloads, verifies and installs the update in the background, then quits (Windows: the installer
        starts the new version) or restarts into it (AppImage)."""
        status = self.status()
        action = status["action"]
        if not action or action["type"] != "install":
            raise ValueError("This installation can't update itself")
        with self._lock:
            if self._install["state"] in ("downloading", "installing"):
                raise RuntimeError("The update is already being installed")
            self._install = {"state": "downloading", "progress": 0, "error": ""}
        with self._lock:
            assets = list(self._latest.get("assets", [])) if self._latest else []
        threading.Thread(target=self._install_worker, args=(status["kind"], action["asset"], assets, quit_app, restart_app),
                         daemon=True, name="update-install").start()

    def _set_install(self, **values) -> None:
        with self._lock:
            self._install.update(values)

    def _install_worker(self, kind: str, asset: dict, assets: list[dict], quit_app, restart_app) -> None:
        try:
            sums = next((a for a in assets if a["name"] == "SHA256SUMS"), None)
            if sums is None:
                raise RuntimeError("The release has no SHA256SUMS - not installing an unverified file")
            with urlopen(_request(sums["url"]), timeout=_TIMEOUT_S) as r:
                expected = checksum_for(r.read().decode("utf-8", "replace"), asset["name"])
            if not expected:
                raise RuntimeError(f"{asset['name']} is not listed in SHA256SUMS")
            if kind == "appimage":
                target = Path(os.environ["APPIMAGE"])
                download = target.with_name(target.name + ".new")      # same folder: the replace below is atomic
            else:
                download = Path(tempfile.gettempdir()) / "falconsdr-update" / asset["name"]
            download.parent.mkdir(parents=True, exist_ok=True)
            self._download(asset, download, expected)
            self._set_install(state="installing", progress=100)
            if kind == "appimage":
                download.chmod(0o755)
                os.replace(download, target)
                restart_app()            # main.py re-execs $APPIMAGE, i.e. the new file
            else:
                _start_windows_installer(download)
                quit_app()               # the installer waits a moment for us, then starts the new version
        except Exception as exc:  # noqa: BLE001 - shown in Settings -> Updates, the running version stays
            self._set_install(state="error", error=str(exc))

    def _download(self, asset: dict, path: Path, expected: str) -> None:
        digest = hashlib.sha256()
        done, total = 0, max(1, asset.get("size") or 1)
        try:
            with urlopen(_request(asset["url"]), timeout=60) as r, path.open("wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    self._set_install(progress=min(99, int(100 * done / total)))
            if digest.hexdigest() != expected:
                raise RuntimeError("The download does not match SHA256SUMS - not installed")
        except BaseException:
            path.unlink(missing_ok=True)
            raise


def _start_windows_installer(setup: Path) -> None:
    """Inno Setup, silent but with its progress window, after this process has quit (it holds the files);
    then the new version through explorer.exe like the Start menu does (gotcha 43)."""
    exe = Path(sys.executable).resolve()
    script = (f'ping -n 6 127.0.0.1 >nul & "{setup}" /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS'
              f' & explorer.exe "{exe}"')
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["cmd.exe", "/c", script], creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
