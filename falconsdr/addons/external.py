"""External decoder programs (rtl_433, dumpvdl2): FalconSDR starts them, hands them the dongle (it stops
scanning first) and reads their JSON lines. Nothing of theirs is bundled or linked - they are separate
programs the user installs, so their licences stay theirs (AGENTS.md gotcha 58)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Callable

from ..audio_transcode import NO_WINDOW, system_env
from ..config import CONFIG_DIR

BIN_DIR = CONFIG_DIR / "addons" / "bin"


def find_program(names: tuple[str, ...], override: str = "", addon_id: str = "") -> str | None:
    """The user's explicit path, else the program on PATH, else our own folder for it."""
    if override:
        return override if Path(override).is_file() else None
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for name in names:
        candidate = BIN_DIR / addon_id / name
        if candidate.is_file():
            return str(candidate)
    return None


class ExternalRunner:
    """One running program; ``on_line`` gets every line of its stdout, ``on_exit`` the exit code."""

    def __init__(self, argv: list[str], on_line: Callable[[str], None], on_exit: Callable[[int], None]):
        self.argv = argv
        self._on_line = on_line
        self._on_exit = on_exit
        self._proc: subprocess.Popen | None = None
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self._stopping = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        kwargs = {"creationflags": NO_WINDOW} if sys.platform == "win32" else {}
        self._proc = subprocess.Popen(
            self.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            env=system_env(), text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
        threading.Thread(target=self._read_stdout, name="addon-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="addon-stderr", daemon=True).start()

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line:
                try:
                    self._on_line(line)
                except Exception:   # noqa: BLE001 - a bad line must not end the reader
                    pass
        code = proc.wait()
        proc.stdout.close()
        self._on_exit(0 if self._stopping else code)

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            self.stderr_tail.append(line.rstrip())
        proc.stderr.close()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping = True
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()


# -- what the programs print -------------------------------------------------------------------------

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_rtl_433(line: str) -> tuple[dict, str] | None:
    """One JSON line of rtl_433 -> (event, summary)."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or "model" not in data:
        return None
    skip = {"time", "model", "id", "channel", "mic", "protocol", "subtype", "rssi", "snr", "noise", "freq", "freq1", "freq2"}
    readings = {k: v for k, v in data.items() if k not in skip and _num(v)}
    shown = ", ".join(f"{k} {v}" for k, v in list(readings.items())[:4])
    who = f"{data['model']}" + (f" #{data['id']}" if "id" in data else "")
    event = {"kind": "sensor", "model": data["model"], "device_id": data.get("id"), "channel": data.get("channel"),
             "fields": {k: v for k, v in data.items() if k not in ("time", "model")}}
    return event, f"{who}: {shown}" if shown else who


def parse_vdl2(line: str) -> tuple[dict, str] | None:
    """One JSON line of dumpvdl2 -> (event, summary). Fields are read defensively: the format has grown."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    v = data.get("vdl2") if isinstance(data, dict) else None
    if not isinstance(v, dict):
        return None
    avlc = v.get("avlc") or {}
    src = (avlc.get("src") or {}).get("addr", "")
    dst = (avlc.get("dst") or {}).get("addr", "")
    acars = avlc.get("acars") or {}
    event = {"kind": "vdl2", "freq_hz": v.get("freq"), "src": src, "dst": dst, "frame_type": avlc.get("frame_type"),
             "sig_level": v.get("sig_level"), "fields": {k: acars[k] for k in acars}}
    if acars:
        event.update(registration=acars.get("reg", "").lstrip("."), label=acars.get("label"), flight=acars.get("flight"),
                     text=acars.get("msg_text"))
        summary = " ".join(x for x in (event["registration"], event.get("flight") or "", f"[{acars.get('label', '')}]") if x)
        if acars.get("msg_text"):
            summary += " " + str(acars["msg_text"]).replace("\n", " ")[:120]
        return event, summary
    return event, f"{src} → {dst} {avlc.get('frame_type', '')}".strip()
