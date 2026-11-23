"""Experimental clear-mode TETRA (ETSI EN 300 392-2) decoder backed by the
bundled zero-dependency Rust binary ``falconsdr-tetra`` (``tetra-rs/``).

Strictly clear-mode only:
- Decodes unencrypted broadcast frames (BSCH SYNC, BNCH SYSINFO: MCC/MNC/CC/LA/AIE),
  clear-mode call signalling (D-SETUP, D-TX GRANTED, D-RELEASE), clear SDS text,
  and clear LIP GPS positions.
- Encrypted calls / networks (encryption_mode != 0, E2EE, or cell AIE) are flagged
  as encrypted and stay muted - nothing is ever decrypted, no keys are accepted.
- Optional clear-mode speech decoding can be wired to a user-supplied external
  ACELP codec helper via ``addons.paths["tetra_codec"]`` (never bundled).
"""
from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

from ..audio_transcode import NO_WINDOW, system_env
from ..i18n import tr
from .external import BIN_DIR, tool_path, which

TETRA_IQ_RATE = 72_000   # 4 samples/symbol at 18 ksym/s
_VOICE_RATE = 8_000
_MAX_BUFFER_S = 1.0
_MAX_QUEUED_EVENTS = 200

TETRA_BIN_DIR = BIN_DIR / "tetra"
_REPO_TETRA_DIR = Path(__file__).resolve().parents[2] / "tetra-rs"


def _exe_name() -> str:
    return "falconsdr-tetra.exe" if sys.platform == "win32" else "falconsdr-tetra"


def tetra_executable(custom_path: str = "") -> Path | None:
    """Single source of truth for locating the ``falconsdr-tetra`` binary."""
    if custom_path:
        p = Path(custom_path).expanduser()
        if p.is_file():
            return p
    name = _exe_name()
    candidates = (
        TETRA_BIN_DIR / name,
        _REPO_TETRA_DIR / "target" / "release" / name,
    )
    for path in candidates:
        if path.is_file():
            return path
    found = shutil.which("falconsdr-tetra")
    return Path(found) if found else None


def is_tetra_installed(custom_path: str = "") -> bool:
    return tetra_executable(custom_path) is not None


def _cargo_bin() -> str | None:
    found = which("cargo")
    if found:
        return found
    home_cargo = Path.home() / ".cargo" / "bin" / ("cargo.exe" if sys.platform == "win32" else "cargo")
    return str(home_cargo) if home_cargo.is_file() else None


def can_build_tetra() -> bool:
    return (_REPO_TETRA_DIR / "Cargo.toml").is_file()


# ---------------------------------------------------------------- slot decoder
class TetraSlotDecoder:
    """One ``falconsdr-tetra --pipe`` process per active TETRA scanner slot.

    ``feed(iq_cf32)`` writes 72 kS/s complex64 samples to stdin; ``read(n)``
    returns ``n`` samples of decoded clear voice (all zeros when no clear voice
    is present so the slot's timeline never drifts); ``drain_events()`` yields
    structured events for ``AddonManager.publish_decoded("tetra", ...)``.
    """

    def __init__(self, freq_hz: int, out_rate: int, custom_path: str = "", codec_path: str = ""):
        exe = tetra_executable(custom_path)
        if exe is None:
            raise RuntimeError("falconsdr-tetra is not installed")
        self.freq_hz = int(freq_hz)
        self.out_rate = int(out_rate)
        self.mcc: int | None = None
        self.mnc: int | None = None
        self.cc: int | None = None
        self.la: int | None = None
        self.talkgroup = ""
        self.source = ""
        self.encrypted = False
        self.events_seen = 0
        self._buf = np.zeros(0, dtype=np.float32)
        self._frac = 0.0
        self._events: list[dict] = []
        self._lock = threading.Lock()
        # Bounded outbound IQ queue: a slow or stalled subprocess (--codec) must never
        # block the scanner engine's slot worker thread (Finding #6).
        self._iq_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=16)

        argv = [str(exe), "--pipe", "--rate", str(TETRA_IQ_RATE), "--freq-hz", str(self.freq_hz)]
        if codec_path:
            cp = Path(codec_path).expanduser()
            if cp.is_file():
                argv += ["--codec", str(cp)]

        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=system_env(), creationflags=NO_WINDOW,
        )
        self._writer_thread = threading.Thread(target=self._write_stdin, daemon=True, name="tetra-tx")
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True, name="tetra-rx")
        self._writer_thread.start()
        self._reader_thread.start()

    def feed(self, iq: np.ndarray) -> None:
        if iq.size == 0:
            return
        raw = np.ascontiguousarray(iq, dtype="<c8").tobytes()
        try:
            self._iq_queue.put_nowait(raw)
        except queue.Full:
            # Drop the oldest queued IQ chunk rather than stalling the real-time scan loop.
            try:
                self._iq_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._iq_queue.put_nowait(raw)
            except queue.Full:
                pass

    def _write_stdin(self) -> None:
        try:
            while True:
                chunk = self._iq_queue.get()
                if chunk is None:
                    return
                if self._proc.stdin is None:
                    return
                self._proc.stdin.write(chunk)
                self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except OSError:
                pass

    def read(self, n: int) -> np.ndarray:
        want = n * _VOICE_RATE / self.out_rate + self._frac
        m = int(want)
        self._frac = want - m
        with self._lock:
            take, self._buf = self._buf[:m], self._buf[m:]
        if take.size < m:
            take = np.concatenate([take, np.zeros(m - take.size, dtype=np.float32)])
        if m == 0 or not take.any():
            return np.zeros(n, dtype=np.float32)
        return np.interp(np.arange(n) * (m / n), np.arange(m), take).astype(np.float32)

    def drain_events(self) -> list[dict]:
        with self._lock:
            if not self._events:
                return []
            out, self._events = self._events, []
            return out

    def close(self) -> None:
        try:
            self._iq_queue.put_nowait(None)
        except queue.Full:
            try:
                self._iq_queue.get_nowait()
                self._iq_queue.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self._proc.kill()
                self._proc.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._writer_thread.join(timeout=0.3)
        self._reader_thread.join(timeout=0.3)

    def _read_stdout(self) -> None:
        try:
            if self._proc.stdout is None:
                return
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                self._handle_message(msg)
        finally:
            try:
                if self._proc.stdout is not None:
                    self._proc.stdout.close()
            except OSError:
                pass

    def _handle_message(self, msg: dict) -> None:
        mtype = str(msg.get("type", ""))
        if mtype == "audio":
            b64 = msg.get("pcm_b64")
            if isinstance(b64, str) and b64:
                try:
                    raw = base64.b64decode(b64)
                    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                except ValueError:
                    return
                if pcm.size:
                    with self._lock:
                        self._buf = np.concatenate([self._buf, pcm])[-int(_MAX_BUFFER_S * _VOICE_RATE):]
            return

        event: dict | None = None
        with self._lock:
            if mtype == "sync":
                self.mcc = int(msg.get("mcc", 0))
                self.mnc = int(msg.get("mnc", 0))
                self.cc = int(msg.get("cc", 0))
                event = {
                    "kind": "tetra", "subtype": "sync",
                    "mcc": self.mcc, "mnc": self.mnc, "cc": self.cc,
                    "ts": int(msg.get("ts", 1)), "cfo_hz": float(msg.get("cfo_hz", 0.0)),
                }
            elif mtype == "sysinfo":
                self.mcc = int(msg.get("mcc", self.mcc or 0))
                self.mnc = int(msg.get("mnc", self.mnc or 0))
                self.cc = int(msg.get("cc", self.cc or 0))
                self.la = int(msg.get("la", 0))
                enc = bool(msg.get("encrypted", False))
                if enc:
                    self.encrypted = True
                event = {
                    "kind": "tetra", "subtype": "sysinfo",
                    "mcc": self.mcc, "mnc": self.mnc, "cc": self.cc, "la": self.la,
                    "carrier": int(msg.get("carrier", 0)),
                    "dl_freq_hz": int(msg.get("dl_freq_hz", 0)),
                    "voice": bool(msg.get("voice", False)),
                    "encrypted": enc,
                }
            elif mtype == "call":
                gssi = int(msg.get("gssi", 0))
                issi = int(msg.get("issi", 0))
                if gssi:
                    self.talkgroup = str(gssi)
                if issi:
                    self.source = str(issi)
                event = {
                    "kind": "tetra", "subtype": "call",
                    "event": str(msg.get("event", "")),
                    "ts": int(msg.get("ts", 1)),
                    "gssi": gssi, "issi": issi,
                    "call_id": int(msg.get("call_id", 0)),
                    "mcc": int(msg.get("mcc", self.mcc or 0)),
                    "mnc": int(msg.get("mnc", self.mnc or 0)),
                }
            elif mtype == "encrypted":
                self.encrypted = True
                gssi = int(msg.get("gssi", 0))
                if gssi:
                    self.talkgroup = str(gssi)
                event = {
                    "kind": "tetra", "subtype": "encrypted",
                    "ts": int(msg.get("ts", 1)),
                    "gssi": gssi,
                    "issi": int(msg.get("issi", 0)),
                    "enc_mode": int(msg.get("enc_mode", 1)),
                    "e2e": bool(msg.get("e2e", False)),
                    "mcc": int(msg.get("mcc", self.mcc or 0)),
                    "mnc": int(msg.get("mnc", self.mnc or 0)),
                }
            elif mtype == "sds":
                event = {
                    "kind": "tetra", "subtype": "sds",
                    "ts": int(msg.get("ts", 1)),
                    "dst": int(msg.get("dst", 0)),
                    "src": int(msg.get("src", 0)),
                    "pid": int(msg.get("pid", 0)),
                    "text": str(msg.get("text", "")),
                    "mcc": int(msg.get("mcc", self.mcc or 0)),
                    "mnc": int(msg.get("mnc", self.mnc or 0)),
                }
            elif mtype == "lip":
                event = {
                    "kind": "tetra", "subtype": "lip",
                    "ts": int(msg.get("ts", 1)),
                    "dst": int(msg.get("dst", 0)),
                    "src": int(msg.get("src", 0)),
                    "lat": float(msg.get("lat", 0.0)),
                    "lon": float(msg.get("lon", 0.0)),
                    "err": int(msg.get("err", 0)),
                    "mcc": int(msg.get("mcc", self.mcc or 0)),
                    "mnc": int(msg.get("mnc", self.mnc or 0)),
                }

            if event is not None:
                self.events_seen += 1
                self._events.append(event)
                if len(self._events) > _MAX_QUEUED_EVENTS:
                    self._events = self._events[-_MAX_QUEUED_EVENTS:]


# ---------------------------------------------------------------- building from source
_build_lock = threading.Lock()
_build_state = {"running": False, "log": [], "error": "", "hint": ""}


def install_state() -> dict:
    with _build_lock:
        return {**_build_state, "log": list(_build_state["log"][-12:])}


def start_build() -> None:
    if not can_build_tetra():
        raise RuntimeError(tr("TETRA source code (tetra-rs/) is not present in this installation."))
    with _build_lock:
        if _build_state["running"]:
            return
        _build_state.update(running=True, log=[], error="", hint="")
    threading.Thread(target=_build_thread, daemon=True, name="tetra-build").start()


def uninstall_tetra() -> None:
    with _build_lock:
        if _build_state["running"]:
            raise RuntimeError(tr("Installation is in progress - wait for it to finish."))
        _build_state.update(log=[], error="", hint="")
    shutil.rmtree(TETRA_BIN_DIR, ignore_errors=True)


def _say(line: str) -> None:
    with _build_lock:
        _build_state["log"].append(line)


def _build_thread() -> None:
    try:
        cargo = _cargo_bin()
        if not cargo:
            with _build_lock:
                _build_state["hint"] = "sudo apt install cargo rustc" if sys.platform.startswith("linux") else "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
            raise RuntimeError(tr("Rust compiler (cargo) was not found."))
        env = dict(system_env() or os.environ)
        env["PATH"] = tool_path()
        cargo_dir = str(Path(cargo).parent)
        if cargo_dir not in env["PATH"].split(os.pathsep):
            env["PATH"] = cargo_dir + os.pathsep + env["PATH"]
        _say("Building falconsdr-tetra (Rust, release)…")
        cmd = [cargo, "build", "--release"]
        if os.name == "posix" and shutil.which("nice"):
            cmd = ["nice", "-n", "15", *cmd]
        proc = subprocess.run(
            cmd, cwd=str(_REPO_TETRA_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, creationflags=NO_WINDOW,
        )
        if proc.returncode != 0:
            for line in proc.stdout.decode("utf-8", "replace").strip().splitlines()[-8:]:
                _say(line)
            raise RuntimeError("cargo build --release failed")
        built = _REPO_TETRA_DIR / "target" / "release" / _exe_name()
        if not built.is_file():
            raise RuntimeError("falconsdr-tetra was built but not found")
        TETRA_BIN_DIR.mkdir(parents=True, exist_ok=True)
        dest = TETRA_BIN_DIR / _exe_name()
        shutil.copy2(built, dest)
        _say("Running --selftest…")
        st = subprocess.run([str(dest), "--selftest"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW, timeout=30)
        if st.returncode != 0:
            raise RuntimeError("falconsdr-tetra --selftest failed")
        _say("Done.")
    except Exception as exc:  # noqa: BLE001
        with _build_lock:
            _build_state["error"] = str(exc)
    finally:
        with _build_lock:
            _build_state["running"] = False
