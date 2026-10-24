"""Owns everything the web UI drives: the persisted config, Memory List and
blacklist, the one ScannerEngine (started/stopped on demand), a thread-safe
snapshot of its live state, the live-audio fan-out and the recording
library. web/server.py is a thin HTTP layer over this.

Engine signals arrive synchronously on engine threads (see
scanner_engine.Signal) - every handler here only takes a lock and stores a
value, so it never holds the scanner up.
"""
from __future__ import annotations

import datetime as dt
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import __version__, autostart, i18n, military_presets, schedule, storage
from .audio_transcode import ffmpeg_available
from .config import DEFAULT_RECORDINGS_DIR, AppConfig, ScheduleEntry, VfoRangeConfig, _from_dict
from .models import BlacklistEntry, Channel, Modulation, snap_airband_hz
from .playback.index import Library
from .playback.transcription_availability import is_transcription_installed
from .scanner_engine import AUDIO_RATE, ScannerEngine
from .sdr_backend import enumerate_devices

# Bounds each listener's backlog so a slow or stalled browser can never make
# the engine wait (old chunks are dropped, not queued forever).
_AUDIO_QUEUE_SIZE = 50
_LOG_SIZE = 100
_SPECTRUM_POINTS = 600
_WORKER_STALE_S = 30.0


def _coerce(current: Any, value: Any) -> Any:
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    return value


# Lists of dataclasses sent whole: field name -> item type.
_LIST_TYPES = {"bands": VfoRangeConfig, "entries": ScheduleEntry}


def merge_patch(obj: Any, patch: dict) -> None:
    """Applies a partial JSON update to a (nested) config dataclass in place,
    coercing each value to the field's current type. Unknown keys are ignored."""
    for key, value in patch.items():
        if not hasattr(obj, key):
            continue
        current = getattr(obj, key)
        if key in _LIST_TYPES and isinstance(value, list):
            setattr(obj, key, [_from_dict(_LIST_TYPES[key], b) for b in value if isinstance(b, dict)])
        elif is_dataclass(current) and isinstance(value, dict):
            merge_patch(current, value)
        elif current is None or value is None:
            setattr(obj, key, value)
        else:
            setattr(obj, key, _coerce(current, value))


def channel_from_dict(d: dict) -> Channel:
    mod = str(d.get("modulation", "AM"))
    return Channel(
        freq_hz=snap_airband_hz(int(d["freq_hz"])), name=str(d.get("name", "")),
        modulation=Modulation(mod) if mod in Modulation._value2member_map_ else Modulation.AM,
        enabled=bool(d.get("enabled", True)), folder=str(d.get("folder", "")), weight=float(d.get("weight", 0.0)),
        ch833=bool(d.get("ch833", False)),
    )


def channel_dict(c: Channel) -> dict:
    return {**asdict(c), "modulation": c.modulation.value}


class Controller:
    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config or AppConfig.load()
        i18n.set_language(self.config.language)
        self.channels: list[Channel] = storage.load_channels()
        self.blacklist: list[BlacklistEntry] = storage.load_blacklist()
        self._lock = threading.Lock()
        self._engine: Optional[ScannerEngine] = None
        self._solo_band: Optional[int] = None
        self._live: dict[str, Any] = self._idle_live()
        self._spectrum: Optional[dict] = None
        self._log: deque[dict] = deque(maxlen=_LOG_SIZE)
        self._log_id = 0
        self._audio_lock = threading.Lock()
        self._audio_subscribers: list[queue.Queue] = []
        self.workers: dict[str, dict] = {}   # name -> last heartbeat (in memory only)
        self.worker_commands: dict[str, list[str]] = {}
        self.library = Library(self.config, self.open_recording_paths, self.freq_names)
        self.log(i18n.tr("Ready."))

    @staticmethod
    def _idle_live() -> dict[str, Any]:
        return {
            "state": "IDLE", "level": -120.0, "frequencies": [], "recording": False,
            "manual_recording": False, "raw_iq": False, "cycle_time": None, "gain_range": [0.0, 50.0],
        }

    def start_background(self) -> None:
        self.library.start()

    def shutdown(self) -> None:
        engine = self._engine
        if engine is not None:
            engine.request_stop()
            engine.join(timeout=5.0)
        self.library.stop()

    # -- helpers used by the library ----------------------------------------
    def freq_names(self) -> dict[int, str]:
        names = {round(c.freq_hz / 1000): c.name for c in self.channels if c.name}
        # Filenames keep whole kHz and the same carrier lands ±1 kHz apart (130.874/130.875).
        for khz, name in list(names.items()):
            names.setdefault(khz - 1, name)
            names.setdefault(khz + 1, name)
        return names

    def open_recording_paths(self) -> set[Path]:
        engine = self._engine
        return engine.open_recording_paths() if engine is not None else set()

    def log(self, message: str) -> None:
        with self._lock:
            if self._log and self._log[-1]["text"] == message:
                return  # e.g. the "wideband scanning" line on every rebuild - it flooded out real events
            self._log_id += 1
            self._log.append({"id": self._log_id, "time": time.time(), "text": message})

    # -- scanner lifecycle ----------------------------------------------------
    @property
    def running(self) -> bool:
        return self._engine is not None

    def start(self) -> None:
        with self._lock:
            existing = self._engine
        if existing is not None:
            if not existing._stop_evt.is_set():
                return
            # Start right after Stop: let the old engine finish first, never
            # two engines fighting over one dongle.
            existing.join(timeout=8.0)
            if existing.is_alive():
                raise RuntimeError(i18n.tr("Scanner is still stopping - try again"))
        engine = ScannerEngine(self.config, self.channels, self.blacklist)
        s = engine.signals
        s.state_changed.connect(lambda v: self._set_live(state=str(v)))
        s.level_changed.connect(lambda v: self._set_live(level=round(float(v), 1)))
        s.freq_changed.connect(lambda v: self._set_live(frequencies=[[int(f), str(n)] for f, n in v]))
        s.recording_changed.connect(lambda active, _p: self._set_live(recording=bool(active)))
        s.manual_recording_changed.connect(lambda active, _p: self._set_live(manual_recording=bool(active)))
        s.raw_iq_recording_changed.connect(lambda active, _p: self._set_live(raw_iq=bool(active)))
        s.cycle_time_changed.connect(lambda v: self._set_live(cycle_time=round(float(v), 2)))
        s.gain_range_changed.connect(lambda lo, hi: self._set_live(gain_range=[float(lo), float(hi)]))
        s.recording_finished.connect(lambda path: self.library.add_file(Path(path)))
        s.spectrum_ready.connect(self._on_spectrum)
        s.live_audio_ready.connect(self._on_live_audio)
        s.blacklisted.connect(self._on_blacklisted)
        s.log.connect(self.log)
        s.finished.connect(lambda: self._on_finished(engine))
        if self._solo_band is not None and self._solo_band < len(self.config.bands):
            engine.set_solo_band(self.config.bands[self._solo_band])
        with self._lock:
            self._engine = engine
            self._live = {**self._idle_live(), "state": "CALCULATE_NEXT_FREQ"}
        engine.start()

    def stop(self) -> None:
        engine = self._engine
        if engine is not None:
            engine.request_stop()

    def _on_finished(self, engine: ScannerEngine) -> None:
        with self._lock:
            if self._engine is not engine:
                return
            self._engine = None
            self._live = self._idle_live()
            self._spectrum = None

    def command(self, name: str, payload: dict) -> None:
        engine = self._engine
        if name == "solo":
            idx = payload.get("band")
            self._solo_band = int(idx) if idx is not None else None
            if engine is not None:
                band = self.config.bands[self._solo_band] if self._solo_band is not None else None
                engine.set_solo_band(band)
            return
        if engine is None:
            raise RuntimeError(i18n.tr("Scanner is not running"))
        enabled = bool(payload.get("enabled"))
        if name == "skip":
            engine.request_skip()
        elif name == "blacklist":
            engine.request_skip_and_blacklist()
        elif name == "manual_record":
            (engine.start_manual_recording if enabled else engine.stop_manual_recording)()
        elif name == "raw_iq":
            (engine.start_raw_iq_recording if enabled else engine.stop_raw_iq_recording)()
        else:
            raise ValueError("Unknown command")

    # -- engine signal handlers (engine threads) ------------------------------
    def _set_live(self, **values: Any) -> None:
        with self._lock:
            self._live.update(values)

    def _on_spectrum(self, freqs: Any, db: Any, trigger_dbfs: float, custom: bool) -> None:
        freqs, db = np.asarray(freqs), np.asarray(db)
        if db.size > _SPECTRUM_POINTS:
            step = int(np.ceil(db.size / _SPECTRUM_POINTS))
            usable = db.size - db.size % step
            db = db[:usable].reshape(-1, step).max(axis=1)
            freqs = freqs[:usable].reshape(-1, step).mean(axis=1)
        with self._lock:
            self._spectrum = {
                "f0": float(freqs[0]) if freqs.size else 0.0,
                "f1": float(freqs[-1]) if freqs.size else 0.0,
                "db": np.round(db, 1).tolist(),
                "trigger": round(float(trigger_dbfs), 1),
                "custom": bool(custom),
            }

    def _on_blacklisted(self, freq_hz: int) -> None:
        entry = BlacklistEntry(
            freq_hz=int(freq_hz), margin_hz=self.config.default_blacklist_margin_hz,
            label="Auto-lockout (SKIP)", date_added=dt.date.today().isoformat(),
        )
        self.set_blacklist(self.blacklist + [entry])

    def _on_live_audio(self, mono: Any) -> None:
        pcm16 = (np.clip(np.asarray(mono, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        with self._audio_lock:
            for q in self._audio_subscribers:
                try:
                    q.put_nowait(pcm16)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(pcm16)
                    except (queue.Empty, queue.Full):
                        pass

    def subscribe_audio(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=_AUDIO_QUEUE_SIZE)
        with self._audio_lock:
            self._audio_subscribers.append(q)
        return q

    def unsubscribe_audio(self, q: queue.Queue) -> None:
        with self._audio_lock:
            if q in self._audio_subscribers:
                self._audio_subscribers.remove(q)

    @property
    def listeners(self) -> int:
        with self._audio_lock:
            return len(self._audio_subscribers)

    # -- snapshots --------------------------------------------------------------
    def live(self, since_log: int = 0) -> dict:
        with self._lock:
            return {
                **self._live,
                "running": self._engine is not None,
                "stopping": self._engine is not None and self._engine._stop_evt.is_set(),
                "solo_band": self._solo_band,
                "schedule_active": schedule.active_indices(self.config),
                "clock": time.strftime("%H:%M"),
                "spectrum": self._spectrum,
                "log": [e for e in self._log if e["id"] > since_log],
                "audio_rate": AUDIO_RATE,
            }

    def settings(self) -> dict:
        return {
            "version": __version__,
            "config": asdict(self.config),
            "channels": [channel_dict(c) for c in self.channels],
            "blacklist": [asdict(b) for b in self.blacklist],
            "presets": [
                {**p.to_dict(), "user": military_presets.is_user_owned_preset(p.id)}
                for p in military_presets.get_all_presets()
            ],
            "devices": self.devices(),
            "languages": [{"code": c, "name": i18n.LANGUAGE_NAMES.get(c, c)} for c in i18n.available_languages()],
            "capabilities": {
                "ffmpeg": ffmpeg_available(),
                "transcription_installed": is_transcription_installed(),
                "platform": sys.platform,
                "autostart": autostart.is_enabled() if autostart.supported() else None,
            },
        }

    def devices(self) -> list[dict]:
        out = [{"driver": "auto", "args": "", "label": i18n.tr("Automatic (first detected)")}]
        for dev in enumerate_devices():
            out.append({
                "driver": dev.get("driver", "auto"),
                "args": ",".join(f"{k}={v}" for k, v in dev.items()),
                "label": dev.get("label") or dev.get("driver", "device"),
            })
        if self.config.debug_mode:
            out.append({"driver": "simulated", "args": "", "label": i18n.tr("Simulation (no hardware)")})
        return out

    # -- edits -------------------------------------------------------------------
    def patch_config(self, patch: dict) -> None:
        old_language = self.config.language
        old_root = self.config.recording.output_dir
        for locked in ("access_token", "workers"):
            patch.pop(locked, None)
        merge_patch(self.config, patch)
        # An emptied folder field would otherwise mean "the current directory".
        self.config.recording.output_dir = self.config.recording.output_dir.strip() or DEFAULT_RECORDINGS_DIR
        self.config.save()
        if self.config.language != old_language:
            i18n.set_language(self.config.language)
        if self.config.recording.output_dir != old_root:
            threading.Thread(target=self.library.rescan, daemon=True).start()
        self._rebuild()

    def set_channels(self, channels: list[Channel]) -> None:
        self.channels = channels
        storage.save_channels(channels)
        engine = self._engine
        if engine is not None:
            engine.update_channels(channels)
            engine.request_rebuild()

    def set_blacklist(self, blacklist: list[BlacklistEntry]) -> None:
        self.blacklist = blacklist
        storage.save_blacklist(blacklist)
        engine = self._engine
        if engine is not None:
            engine.update_blacklist(blacklist)

    def _rebuild(self) -> None:
        engine = self._engine
        if engine is not None:
            if self._solo_band is not None and self._solo_band >= len(self.config.bands):
                self._solo_band = None
            engine.set_solo_band(self.config.bands[self._solo_band] if self._solo_band is not None else None)
            engine.request_rebuild()

    def mark_disclaimer_seen(self, kind: str, lang: str) -> None:
        seen = (self.config.legal_disclaimer.launch_seen_languages if kind == "launch"
                else self.config.legal_disclaimer.recording_seen_languages)
        if lang not in seen:
            seen.append(lang)
            self.config.save()

    def reset_to_defaults(self) -> None:
        """Settings, Memory List, blacklist and custom/hidden presets back to a
        fresh install. Recordings (and where they live), the access password
        and connected computers are kept - a reset must never lock anyone out
        or orphan a library."""
        self.stop()
        fresh = AppConfig()
        fresh.recording.output_dir = self.config.recording.output_dir
        fresh.access_token = self.config.access_token
        fresh.workers = self.config.workers
        fresh.legal_disclaimer = self.config.legal_disclaimer
        for f in fresh.__dataclass_fields__:
            setattr(self.config, f, getattr(fresh, f))
        self.config.save()
        self.set_channels([])
        self.set_blacklist([])
        military_presets.save_user_data([], set())

    # -- workers (transcription / local copies) ---------------------------------
    def worker_settings(self, name: str) -> dict:
        defaults = {"transcribe": True, "mirror": False, "mirror_dir": ""}
        return {**defaults, **self.config.workers.get(name, {})}

    def heartbeat(self, name: str, info: dict) -> dict:
        info = {**info, "last_seen": time.time()}
        with self._lock:
            self.workers[name] = info
            commands = self.worker_commands.pop(name, [])
        return {
            "settings": self.worker_settings(name),
            "transcription": asdict(self.config.transcription),
            "commands": commands,
        }

    def update_worker(self, name: str, settings: dict) -> None:
        merged = self.worker_settings(name)
        for key in ("transcribe", "mirror"):
            if key in settings:
                merged[key] = bool(settings[key])
        if "mirror_dir" in settings:
            merged["mirror_dir"] = str(settings["mirror_dir"]).strip()
        self.config.workers[name] = merged
        self.config.save()

    def send_worker_command(self, name: str, command: str) -> None:
        if command not in ("install", "install_gpu", "uninstall", "uninstall_with_models", "upload_local", "detach"):
            raise ValueError("Unknown worker command")
        with self._lock:
            self.worker_commands.setdefault(name, []).append(command)

    def forget_worker(self, name: str) -> None:
        with self._lock:
            self.workers.pop(name, None)
        self.config.workers.pop(name, None)
        self.config.save()

    def workers_view(self) -> list[dict]:
        now = time.time()
        with self._lock:
            names = set(self.workers) | set(self.config.workers)
            out = []
            for name in sorted(names):
                info = self.workers.get(name, {})
                out.append({
                    "name": name,
                    "online": now - info.get("last_seen", 0) < _WORKER_STALE_S,
                    "info": info,
                    "settings": self.worker_settings(name),
                })
        return out
