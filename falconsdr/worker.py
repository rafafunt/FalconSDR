"""A computer helping a FalconSDR server: transcribes recordings with its
own Whisper (often the only machine with a GPU) and/or keeps a local copy
of the library. The same class also runs inside every server process
(``embedded``) so a machine that both scans and transcribes needs nothing
extra - it just talks to its own HTTP API over localhost.

Everything is pulled from the server, whose web UI is where the user turns
transcription/copying on or off per computer (heartbeat replies carry the
settings and one-shot commands: install transcription, upload this
computer's own recordings, detach).

Copies are verified by size before being reported as mirrored - the server
may only rely on (e.g. auto-delete after) a copy that is really complete.
"""
from __future__ import annotations

import json
import logging
import shutil
import socket
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .audio_transcode import system_env
from .config import CONFIG_DIR, TranscriptionConfig, _from_dict, atomic_write_text
from .playback.models import QualityMetrics, Recording
from .playback.organizer import iter_all_recordings, parse_filename
from .playback.transcription_availability import is_transcription_installed
from .playback.transcription_install import removal_pending

logger = logging.getLogger(__name__)

_HEARTBEAT_S = 5.0
_MIRROR_INTERVAL_S = 60.0
_IDLE_S = 10.0
_MODEL_RETRY_S = 60.0  # a model that failed to load (still downloading...) is tried again after this
STATE_FILE = CONFIG_DIR / "worker_state.json"


class Api:
    def __init__(self, base_url: str, token: "str | Callable[[], str]") -> None:
        self.base = base_url.rstrip("/")
        self._token = token  # a callable is read on every request: the server's own password can change while it runs

    @property
    def token(self) -> str:
        return self._token() if callable(self._token) else self._token

    def _req(self, method: str, path: str, data: Optional[bytes] = None, headers: Optional[dict] = None,
             timeout: float = 30.0):
        h = {"Authorization": f"Bearer {self.token}", "X-FalconSDR": "1", **(headers or {})}
        return urlopen(Request(self.base + path, data=data, headers=h, method=method), timeout=timeout)

    def get(self, path: str):
        with self._req("GET", path) as r:
            return json.loads(r.read() or b"null")

    def post(self, path: str, body: dict, timeout: float = 30.0):
        data = json.dumps(body).encode()
        with self._req("POST", path, data, {"Content-Type": "application/json"}, timeout) as r:
            return json.loads(r.read() or b"null")

    def download(self, rel: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        try:
            with self._req("GET", "/api/recording?id=" + quote(rel), timeout=60) as r, partial.open("wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    def upload(self, rel: str, source: Path) -> None:
        st = source.stat()
        with source.open("rb") as f:
            req = Request(
                self.base + "/api/worker/upload?id=" + quote(rel), data=f, method="PUT",
                headers={"Authorization": f"Bearer {self.token}", "X-FalconSDR": "1", "Content-Length": str(st.st_size),
                         "X-Mtime": str(st.st_mtime), "Content-Type": "application/octet-stream"},
            )
            with urlopen(req, timeout=300) as r:
                r.read()


class Worker(threading.Thread):
    def __init__(self, base_url: str, token: "str | Callable[[], str]", name: str = "", local_root: Optional[Path] = None,
                 embedded: bool = False, on_detach: Optional[Callable[[], None]] = None,
                 on_restart: Optional[Callable[[], None]] = None) -> None:
        super().__init__(daemon=True, name="worker")
        self.api = Api(base_url, token)
        self.name = name or socket.gethostname()
        self.local_root = Path(local_root) if local_root else None
        self.embedded = embedded
        self.on_detach = on_detach
        self.on_restart = on_restart
        self._stop = threading.Event()
        self._wake = threading.Event()  # a command arrived - don't sit out the idle pause
        self._log: deque[str] = deque(maxlen=40)
        self._status = "idle"
        self._settings: dict = {"transcribe": False, "mirror": False, "mirror_dir": ""}
        self._tconfig = TranscriptionConfig()
        self._commands: list[str] = []
        self._lock = threading.Lock()
        self._installed = is_transcription_installed()
        self._local_only = 0
        self._last_mirror = 0.0
        self._connected = False

    # -- status reported to the server ------------------------------------------
    def say(self, line: str) -> None:
        self._log.append(f"{datetime.now():%H:%M:%S} {line}")
        logger.info("worker: %s", line)

    def _info(self) -> dict:
        return {
            "embedded": self.embedded, "status": self._status, "installed": self._installed,
            "gpu": _gpu_name(), "log": list(self._log), "local_only": self._local_only,
            "mirror_root": str(self._mirror_root() or ""), "removal_pending": removal_pending(),
        }

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                reply = self.api.post("/api/worker/heartbeat", {"name": self.name, "info": self._info()}, timeout=10)
                with self._lock:
                    self._settings = reply["settings"]
                    self._tconfig = _from_dict(TranscriptionConfig, reply["transcription"])
                    self._commands += reply.get("commands", [])
                    if self._commands:
                        self._wake.set()
                if not self._connected:
                    self._connected = True
                    self.say("connected")
            except (URLError, OSError, ValueError, KeyError) as exc:
                if self._connected:
                    self.say(f"server unreachable: {exc}")
                self._connected = False
            self._stop.wait(_HEARTBEAT_S)

    # -- main loop ------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="worker-heartbeat").start()
        imported = False
        while not self._stop.is_set():
            if not self._connected:
                self._stop.wait(2.0)
                continue
            try:
                self._run_commands()
                if not self.embedded and not imported:
                    imported = self._import_local_metadata()
                did_work = False
                if not self.embedded and time.monotonic() - self._last_mirror > _MIRROR_INTERVAL_S:
                    self._last_mirror = time.monotonic()
                    self._mirror() if self._settings.get("mirror") else self._count_local_only()
                if self._settings.get("transcribe") and self._installed:
                    did_work = self._transcribe_one()
                self._status = "transcribing" if did_work else "idle"
                if not did_work:
                    self._wake.wait(_IDLE_S)
                    self._wake.clear()
            except (URLError, OSError, ValueError, KeyError) as exc:
                self.say(f"error: {exc}")
                self._stop.wait(_IDLE_S)

    def _run_commands(self) -> None:
        with self._lock:
            commands, self._commands = self._commands, []
        for command in commands:
            if command in ("install", "install_gpu"):
                self._install(with_gpu=command == "install_gpu")
            elif command in ("uninstall", "uninstall_with_models"):
                self._uninstall(with_models=command == "uninstall_with_models")
            elif command == "restart" and self.on_restart:
                self.say("restarting FalconSDR")
                self.on_restart()
            elif command == "upload_local":
                self._upload_local()
            elif command == "detach":
                self.say("detached from the server")
                self.stop()
                if self.on_detach:
                    self.on_detach()

    # -- transcription ----------------------------------------------------------
    def _install(self, with_gpu: bool) -> None:
        from .playback.transcription_install import install_transcription
        self._status = "installing"
        ok, needs_restart, message = install_transcription(with_gpu, lambda line: self._log.append(line[-300:]))
        self._installed = is_transcription_installed()
        if ok and needs_restart:
            self.say("installed - restart FalconSDR on this computer to start transcribing")
        else:
            self.say("transcription installed" if ok else f"install failed: {message[-300:]}")
        self._status = "idle"

    def _uninstall(self, with_models: bool) -> None:
        from .playback.transcription_install import uninstall_transcription
        self._status = "uninstalling"
        with self._lock:
            tconfig = self._tconfig
        models = [m for m in (tconfig.generic_model, tconfig.english_atc_model) if m] if with_models else []
        ok, needs_restart, message = uninstall_transcription(models, lambda line: self._log.append(line[-300:]))
        # Off right away, also when the files only go at the next start.
        self._installed = False if ok else is_transcription_installed()
        if ok and needs_restart:
            self.say("transcription uninstalled - restart FalconSDR to remove its files")
        else:
            self.say("transcription uninstalled" if ok else f"uninstall failed: {message[-300:]}")
        self._status = "idle"

    def _transcribe_one(self) -> bool:
        from .playback.transcription_engine import ModelUnavailable, transcribe_recording
        job = self.api.post("/api/worker/claim", {"name": self.name})["job"]
        if not job:
            return False
        rel = job["id"]
        self._status = "transcribing"
        temp_dir = None
        try:
            local = Path(job["path"]) if self.embedded else None
            mirror_root = self._mirror_root()
            if local is None and mirror_root is not None and safe_rel(rel):
                candidate = mirror_root / rel
                if candidate.is_file() and candidate.stat().st_size == job["size"]:
                    local = candidate
            if local is None or not local.is_file():
                temp_dir = Path(tempfile.mkdtemp(prefix="falconsdr-"))
                local = temp_dir / Path(rel).name
                self.api.download(rel, local)
            now = datetime.now(timezone.utc)
            rec = Recording(path=local, dt_local=now, dt_utc=now, freq_khz=0,
                            quality=QualityMetrics(0, 0, 0, 0, 0, float(job.get("duration") or 0)))
            with self._lock:
                tconfig = self._tconfig
            result = transcribe_recording(rec, tconfig, forced_language=job.get("language"))
            self.api.post("/api/worker/result", {"id": rel, "key": job["key"], "result": result.__dict__})
            self.say(f"transcribed {Path(rel).name} [{result.status}]")
        except ModelUnavailable as exc:
            # Back to the queue, untouched - the next try loads the model afresh.
            self.api.post("/api/worker/release", {"id": rel})
            self.say(f"transcription model not available yet ({exc}) - retrying in {_MODEL_RETRY_S:.0f} s")
            self._wake.wait(_MODEL_RETRY_S)  # a command (e.g. Uninstall) still gets through
            self._wake.clear()
            return False
        except BaseException:
            try:
                self.api.post("/api/worker/release", {"id": rel})
            except (URLError, OSError):
                pass
            raise
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
        return True

    # -- local copies -------------------------------------------------------------
    def _mirror_root(self) -> Optional[Path]:
        configured = (self._settings.get("mirror_dir") or "").strip()
        return Path(configured).expanduser() if configured else self.local_root

    def _local_files(self, root: Path) -> dict[str, Path]:
        out = {}
        for path in iter_all_recordings(root):
            try:
                out[path.relative_to(root).as_posix()] = path
            except ValueError:
                continue
        return out

    def _count_local_only(self) -> None:
        root = self._mirror_root()
        if root is None or not root.is_dir():
            self._local_only = 0
            return
        server = {f["id"] for f in self.api.get("/api/worker/files")}
        self._local_only = sum(1 for rel in self._local_files(root) if rel not in server)

    def _mirror(self) -> None:
        root = self._mirror_root()
        if root is None:
            return
        server = {f["id"]: f for f in self.api.get("/api/worker/files")}
        local = self._local_files(root) if root.is_dir() else {}
        self._local_only = sum(1 for rel in local if rel not in server)
        verified = []
        for rel, f in sorted(server.items(), key=lambda kv: kv[0], reverse=True):
            if self._stop.is_set():
                break
            if not safe_rel(rel):
                self.say(f"ignored a suspicious path from the server: {rel[:80]}")
                continue
            target = root / rel
            if not (target.is_file() and target.stat().st_size == f["size"]):
                self._status = "copying"
                self.api.download(rel, target)
            if target.is_file() and target.stat().st_size == f["size"]:
                verified.append(rel)
            if len(verified) >= 500:
                self.api.post("/api/worker/mirrored", {"ids": verified})
                verified = []
        if verified:
            self.api.post("/api/worker/mirrored", {"ids": verified})

    def _upload_local(self) -> None:
        """Sends this computer's own recordings the server doesn't have (e.g.
        an existing library from before the web UI), with their metadata."""
        root = self._mirror_root()
        if root is None or not root.is_dir():
            self.say("no local recordings folder to upload from")
            return
        server = {f["id"]: f["size"] for f in self.api.get("/api/worker/files")}
        todo = [(rel, path) for rel, path in self._local_files(root).items() if rel not in server]
        self._status = "uploading"
        self.say(f"uploading {len(todo)} recordings")
        for i, (rel, path) in enumerate(todo, 1):
            if self._stop.is_set():
                break
            try:
                self.api.upload(rel, path)
            except HTTPError as exc:
                self.say(f"skipped {rel}: {exc.code}")
            if i % 500 == 0:
                self.say(f"uploaded {i}/{len(todo)}")
        self._import_local_metadata(force=True)
        self._local_only = 0
        self.say("upload finished")
        self._status = "idle"

    def _import_local_metadata(self, force: bool = False) -> bool:
        """One-time: this computer's own transcripts, quality analysis and
        favorites (the desktop app kept them locally) for files the server
        also has. Never overwrites anything the server already knows."""
        root = self._mirror_root()
        state = _load_state()
        done_key = f"imported:{self.api.base}"
        if root is None or (state.get(done_key) and not force):
            return True
        from .playback.favorites_store import FavoritesStore
        from .playback.quality_cache import QualityCache
        from .playback.transcription_cache import TranscriptionCache
        transcripts, quality = TranscriptionCache()._data, QualityCache()._data
        favorites = FavoritesStore()._keys
        if not (transcripts or quality or favorites):
            state[done_key] = True
            _save_state(state)
            return True
        server = {f["id"]: f["size"] for f in self.api.get("/api/worker/files")}
        items: dict[str, dict] = {}
        for key in set(transcripts) | set(quality) | set(favorites):
            path_str, _, rest = key.rpartition("|")
            path_str, _, size = path_str.rpartition("|")
            try:
                rel = Path(path_str).relative_to(root).as_posix()
            except ValueError:
                continue
            if server.get(rel) != int(size) or parse_filename(Path(rel).name) is None:
                continue
            item = items.setdefault(rel, {"id": rel, "size": int(size)})
            if key in transcripts:
                item["transcript"] = transcripts[key]
            if key in quality:
                item["quality"] = quality[key]
            if key in favorites:
                item["favorite"] = True
        batch = list(items.values())
        imported = 0
        for i in range(0, len(batch), 2000):
            imported += self.api.post("/api/worker/import", {"items": batch[i:i + 2000]}, timeout=120)["imported"]
        if batch:
            self.say(f"imported metadata of {len(batch)} recordings ({imported} transcripts)")
        state[done_key] = True
        _save_state(state)
        return True


def safe_rel(rel: str) -> bool:
    """A recording id from the server, as a path below our folder: relative, no "..",
    and a name FalconSDR itself would have written. The server is trusted only so far."""
    path = Path(rel)
    return not path.is_absolute() and ".." not in path.parts and parse_filename(path.name) is not None


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    atomic_write_text(STATE_FILE, json.dumps(state))


def _gpu_name() -> str:
    """Best-effort NVIDIA GPU name for the UI ("" if none / unknown)."""
    global _GPU_CACHE
    if _GPU_CACHE is None:
        _GPU_CACHE = ""
        if shutil.which("nvidia-smi"):
            import subprocess
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                     capture_output=True, text=True, errors="replace", timeout=5,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=system_env())
                _GPU_CACHE = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
            except (OSError, subprocess.TimeoutExpired):
                pass
    return _GPU_CACHE


_GPU_CACHE: Optional[str] = None
