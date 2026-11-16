"""External decoder programs (rtl_433, dumpvdl2): FalconSDR starts them, hands them the dongle (it stops
scanning first) and reads their JSON lines. Nothing of theirs is bundled or linked - they are separate
programs the user installs, so their licences stay theirs (AGENTS.md gotcha 58)."""
from __future__ import annotations

import json
import os
import re
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


def tool_path() -> str:
    """PATH for finding and running tools. A macOS app started from the Finder gets only /usr/bin:/bin, without Homebrew."""
    path = os.environ.get("PATH", "")
    if sys.platform == "darwin":
        path = os.pathsep.join(filter(None, [path, "/opt/homebrew/bin", "/usr/local/bin"]))
    return path


def which(name: str) -> str | None:
    return shutil.which(name, path=tool_path())


def find_program(names: tuple[str, ...], override: str = "", addon_id: str = "") -> str | None:
    """The user's explicit path, else the copy FalconSDR installed (or the user put in its folder), else the program on PATH."""
    if override:
        return override if Path(override).is_file() else None
    for name in names:
        for candidate in (BIN_DIR / addon_id / name, BIN_DIR / addon_id / "prefix" / "bin" / name):
            if candidate.is_file():
                return str(candidate)
    for name in names:
        found = which(name)
        if found:
            return found
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

_PROTOCOLS: dict[str, list[dict]] = {}


def list_rtl433_protocols(program: str) -> list[dict]:
    """The decoders of the installed rtl_433: [{"id", "name", "default"}] (default False = off unless asked for). Asked
    of the program itself (``rtl_433 -R help``, which does not open the receiver), so numbers always fit its version."""
    if program in _PROTOCOLS:
        return _PROTOCOLS[program]
    kwargs = {"creationflags": NO_WINDOW} if sys.platform == "win32" else {}
    try:
        out = subprocess.run([program, "-R", "help"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=15, env=system_env(), stdin=subprocess.DEVNULL, **kwargs)
        text = out.stderr + out.stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in text.splitlines():
        m = re.match(r"^\s*\[(\d+)\](\*?)\s*(.+?)\s*$", line)
        if m:
            found.append({"id": int(m.group(1)), "name": m.group(3), "default": not m.group(2) and not m.group(3).startswith("*")})
    if found:
        _PROTOCOLS[program] = found
    return found


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_rtl_433(line: str, allow_remotes: bool = False) -> tuple[dict, str] | None:
    """One JSON line of rtl_433 -> (event, summary). The kind of device and what it measures come from sensors.py;
    remote controls and key fobs (somebody's car or garage) are dropped unless asked for, and never keep their codes."""
    from .sensors import ALIASES, interpret, redact
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or "model" not in data:
        return None
    reported = str(data["model"])
    data["model"] = ALIASES.get(reported, reported)
    info = interpret(data["model"], data)
    if info["category"] == "remote" and not allow_remotes:
        return None
    fields = redact({k: v for k, v in data.items() if k not in ("time", "model")}, info["category"])
    if reported != data["model"]:
        fields["model_reported"] = reported
    who = f"{data['model']}" + (f" #{data['id']}" if "id" in data else "")
    event = {"kind": "sensor", "model": data["model"], "device_id": data.get("id"), "channel": data.get("channel"),
             "category": info["category"], "readings": info["readings"], "status": info["status"], "primary": info["primary"], "fields": fields}
    shown = ", ".join((r["label"] + " " if r["label"].split()[0] not in ("Temperature", "Humidity") else "") + f"{r['value']} {r['unit']}".strip() for r in info["readings"][:3])
    if not shown and info["status"]:
        shown = ", ".join(f"{x['key']} {x['value']}" for x in info["status"][:3])
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
    # "layer" lets the UI hide the link-layer chatter (acknowledgements, polls) that is most of what VDL2 carries:
    # an ACARS block, or any application payload next to the control fields, is "data"; the rest is "link".
    control = {"src", "dst", "cr", "frame_type", "cmd", "rseq", "sseq", "poll", "pf", "retransmission", "sig_level"}
    layer = "acars" if acars else ("data" if any(k not in control for k in avlc) else "link")
    event = {"kind": "vdl2", "layer": layer, "freq_hz": v.get("freq"), "src": src, "dst": dst, "frame_type": avlc.get("frame_type"),
             "sig_level": v.get("sig_level"), "fields": {k: acars[k] for k in acars}}
    if acars:
        event.update(registration=acars.get("reg", "").lstrip("."), label=acars.get("label"), flight=acars.get("flight"),
                     text=acars.get("msg_text"))
        summary = " ".join(x for x in (event["registration"], event.get("flight") or "", f"[{acars.get('label', '')}]") if x)
        if acars.get("msg_text"):
            summary += " " + str(acars["msg_text"]).replace("\n", " ")[:120]
        return event, summary
    return event, f"{src} → {dst} {avlc.get('frame_type', '')}".strip()
