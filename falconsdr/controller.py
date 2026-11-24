"""Owns everything the web UI drives: the persisted config, Memory List and
blacklist, the one ScannerEngine (started/stopped on demand), a thread-safe
snapshot of its live state, the live-audio fan-out and the recording
library. web/server.py is a thin HTTP layer over this.

Engine signals arrive synchronously on engine threads (see
scanner_engine.Signal) - every handler here only takes a lock and stores a
value, so it never holds the scanner up.
"""
from __future__ import annotations

import copy
import datetime as dt
import queue
import sys
import threading
import traceback
import time
from collections import deque
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import __version__, antenna_range, autostart, digital_voice, i18n, military_presets, schedule, sdr_probe, storage
from .audio_transcode import ffmpeg_available
from .config import DEFAULT_RECORDINGS_DIR, AppConfig, ScheduleEntry, VfoRangeConfig, _from_dict
from .models import BlacklistEntry, Channel, Modulation, snap_airband_hz
from .playback.index import Library
from .playback.transcription_availability import is_transcription_installed
from . import sdr_native
from .addons.manager import AddonManager
from .addons import tetra as tetra_addon
from .notifier import Notifier, clean_channel, clean_rule, mask
from .updates import UpdateChecker
from .scanner_engine import AUDIO_RATE, ScannerEngine, SharedFrequencies
from .sdr_backend import HAVE_SOAPY, RTLTCP_DRIVER, device_family, device_method, enumerate_devices

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
# Settings the engine reads from the config on every tick: changing them needs no new scan plan.
_READ_LIVE_KEYS = {"trigger_level_dbfs", "squelch_enabled", "squelch_offset_db"}
_ACTIVE = ("SIGNAL_DETECTED", "LINGER")
# With several dongles the status shown is the most telling of their states.
_STATE_RANK = ["SIGNAL_DETECTED", "LINGER", "RECONNECTING", "SCANNING", "TUNING", "CALCULATE_NEXT_FREQ", "IDLE"]


def _nullable(obj: Any, key: str) -> bool:
    return any(f.name == key and "None" in str(f.type) for f in fields(obj))


def merge_patch(obj: Any, patch: dict) -> None:
    """Applies a partial JSON update to a (nested) config dataclass in place,
    coercing each value to the field's current type. Unknown keys are ignored.
    A value that can't be coerced raises (ValueError/TypeError) - patch_config()
    tries it on a copy first, so a bad request never half-applies."""
    for key, value in patch.items():
        if not hasattr(obj, key):
            continue
        current = getattr(obj, key)
        if key in _LIST_TYPES and isinstance(value, list):
            setattr(obj, key, [_from_dict(_LIST_TYPES[key], b) for b in value if isinstance(b, dict)])
        elif is_dataclass(current) and isinstance(value, dict):
            merge_patch(current, value)
        elif current is None:
            setattr(obj, key, value)
        elif value is None:
            if not _nullable(obj, key):
                raise ValueError(f"{key} can't be empty")
            setattr(obj, key, None)
        else:
            setattr(obj, key, _coerce(current, value))


def _opt_int(val: Any) -> int | None:
    return int(val) if val is not None and val != "" else None


def _opt_float(val: Any) -> float | None:
    return float(val) if val is not None and val != "" else None


def _opt_bool(val: Any) -> bool | None:
    return bool(val) if val is not None and val != "" else None


def channel_from_dict(d: dict) -> Channel:
    mod = str(d.get("modulation", "AM"))
    bw = _opt_int(d.get("bandwidth_hz"))
    sq_off = _opt_float(d.get("squelch_offset_db"))
    linger = _opt_int(d.get("linger_time_ms"))
    nr_str = _opt_float(d.get("noise_reduction_strength"))
    return Channel(
        freq_hz=snap_airband_hz(int(d["freq_hz"])), name=str(d.get("name", "")),
        modulation=Modulation(mod) if mod in Modulation._value2member_map_ else Modulation.AM,
        enabled=bool(d.get("enabled", True)), folder=str(d.get("folder", "")), weight=float(d.get("weight", 0.0)),
        ch833=bool(d.get("ch833", False)),
        bandwidth_hz=max(1000, bw) if bw is not None else None,
        trigger_level_dbfs=_opt_float(d.get("trigger_level_dbfs")),
        squelch_enabled=_opt_bool(d.get("squelch_enabled")),
        squelch_offset_db=min(0.0, sq_off) if sq_off is not None else None,
        linger_time_ms=max(0, linger) if linger is not None else None,
        noise_reduction_enabled=_opt_bool(d.get("noise_reduction_enabled")),
        noise_reduction_strength=min(1.0, max(0.0, nr_str)) if nr_str is not None else None,
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
        self._start_lock = threading.Lock()  # two Start requests at once must not start two sets of engines
        # One engine per dongle (config.device + device.extra_devices); the
        # first is the "lead". _audible = index of the one heard live.
        self._engines: list[ScannerEngine] = []
        self._finished: set[int] = set()
        self._lives: list[dict[str, Any]] = []
        self._audible = 0
        self._solo_band: Optional[int] = None
        self._spectrum: Optional[dict] = None
        self._log: deque[dict] = deque(maxlen=_LOG_SIZE)
        self._log_id = 0
        self._audio_lock = threading.Lock()
        self._audio_subscribers: list[queue.Queue] = []
        self.workers: dict[str, dict] = {}   # name -> last heartbeat (in memory only)
        self.worker_commands: dict[str, list[str]] = {}
        self.library = Library(self.config, self.open_recording_paths, self.freq_names)
        self.addons = AddonManager(self.config, self.log, self._addon_ended)
        self.updates = UpdateChecker(self.config)
        self.notifier = Notifier(self.config, lambda: (float(self.config.location.home_lat or 0), float(self.config.location.home_lon or 0)))
        self.addons.hub.on_event = self.notifier.on_message
        self.addons.on_failed = lambda _id, why: self.notifier.on_system("addon_failed", why)
        self.library.on_transcript = self.notifier.on_transcript
        self._held: set[int] = set()            # receivers an external add-on holds (antenna_range.device_ranges reads it)
        self.config._held_dongles = self._held
        self._addon_dongles: dict[str, int] = {}     # running external add-on -> the receiver it holds
        self._sched_started: dict[str, int] = {}     # the ones the schedule started (it stops them again)
        self._sched_suppressed: dict[str, tuple] = {}  # stopped by the user while scheduled: not restarted until the range ends
        self._bg_stop = threading.Event()
        self._shared: Optional[SharedFrequencies] = None
        self._resume = False    # one receiver only: the scanner was running when an add-on took it
        self._known_antennas: list[str] = []
        self._devices_cache: tuple[float, list[dict], list[str]] | None = None
        self.log(i18n.tr("Ready."))

    @staticmethod
    def _idle_live() -> dict[str, Any]:
        return {
            "state": "IDLE", "level": -120.0, "frequencies": [], "recording": False,
            "manual_recording": False, "raw_iq": False, "hold": False, "cycle_time": None,
            "gain_range": [0.0, 50.0], "antennas": [],
        }

    def start_background(self) -> None:
        self.library.start()
        threading.Thread(target=self._schedule_loop, daemon=True, name="schedule-addons").start()

    @staticmethod
    def _join(engine: ScannerEngine, timeout: float) -> None:
        if engine.ident is not None:    # a receiver an add-on holds has an engine that was never started
            engine.join(timeout=timeout)

    def shutdown(self) -> None:
        self._bg_stop.set()
        self.addons.stop_external()
        self.addons.stats.flush(force=True)
        engines = self._engines
        for engine in engines:
            engine.request_stop()
        for engine in engines:
            self._join(engine, 5.0)
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
        with self._lock:
            engines = list(self._engines)
        return set().union(*(engine.open_recording_paths() for engine in engines))

    def log(self, message: str) -> None:
        with self._lock:
            if self._log and self._log[-1]["text"] == message:
                return  # e.g. the "wideband scanning" line on every rebuild - it flooded out real events
            self._log_id += 1
            self._log.append({"id": self._log_id, "time": time.time(), "text": message})

    # -- scanner lifecycle ----------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._engines)

    def _devices(self) -> list[Optional[dict]]:
        """None = config.device itself, then every usable extra dongle."""
        extra = [d for d in self.config.device.extra_devices if isinstance(d, dict) and d.get("driver")]
        return [None, *extra]

    def start(self) -> None:
        with self._start_lock:
            self._start()

    def _start(self) -> None:
        if sdr_probe.state()["running"]:
            raise RuntimeError(i18n.tr("The receiver test is still running - wait for it to finish."))
        devices = self._devices()
        held = {k for k in self._held if k < len(devices)}
        if held and len(held) >= len(devices):
            raise RuntimeError(i18n.tr("An add-on is using the receiver - stop it first."))
        with self._lock:
            existing = list(self._engines)
        if existing:
            if not all(e._stop_evt.is_set() for e in existing):
                return
            # Start right after Stop: let the old engines finish first, never
            # two engines fighting over one dongle.
            for e in existing:
                self._join(e, 8.0)
            if any(e.is_alive() for e in existing):
                raise RuntimeError(i18n.tr("Scanner is still stopping - try again"))
        shared = SharedFrequencies() if len(devices) > 1 else None
        self._shared = shared
        engines = [
            ScannerEngine(self.config, self.channels, self.blacklist, device=dev, share=(k, len(devices)), shared=shared)
            for k, dev in enumerate(devices)
        ]
        for k, engine in enumerate(engines):
            engine.addons = self.addons
            self._connect(k, engine, len(engines))
            if self._solo_band is not None and self._solo_band < len(self.config.bands):
                engine.set_solo_band(self.config.bands[self._solo_band])
        with self._lock:
            # A held receiver keeps an engine that is never started (so the lists stay aligned with the receivers).
            self._engines, self._finished, self._audible = engines, set(held), min(set(range(len(engines))) - held)
            self._lives = [{**self._idle_live(), "state": "CALCULATE_NEXT_FREQ"} for _ in engines]
        for k, engine in enumerate(engines):
            if k not in held:
                engine.start()

    def _connect(self, k: int, engine: ScannerEngine, count: int) -> None:
        s = engine.signals
        s.state_changed.connect(lambda v: self._set_live(k, state=str(v)))
        s.level_changed.connect(lambda v: self._set_live(k, level=round(float(v), 1)))
        s.freq_changed.connect(lambda v: self._set_live(k, frequencies=[[int(f), str(n)] for f, n in v]))
        s.recording_changed.connect(lambda active, _p: self._set_live(k, recording=bool(active)))
        s.manual_recording_changed.connect(lambda active, _p: self._set_live(k, manual_recording=bool(active)))
        s.hold_changed.connect(lambda v: self._set_live(k, hold=bool(v)))
        s.raw_iq_recording_changed.connect(lambda active, _p: self._set_live(k, raw_iq=bool(active)))
        s.cycle_time_changed.connect(lambda v: self._set_live(k, cycle_time=round(float(v), 2)))
        s.gain_range_changed.connect(lambda lo, hi: self._set_live(k, gain_range=[float(lo), float(hi)]))
        s.antennas_changed.connect(lambda ants: self._on_antennas(k, ants))
        s.recording_finished.connect(lambda path: self.library.add_file(Path(path)))
        s.recording_finished.connect(self._notify_recording)
        s.spectrum_ready.connect(lambda *a: self._on_spectrum(k, *a))
        s.live_audio_ready.connect(lambda mono: self._on_live_audio(k, mono))
        s.blacklisted.connect(self._on_blacklisted)
        s.async_read_disabled.connect(self.config.save)
        s.log.connect(self.log if count == 1 else (lambda msg: self.log(f"SDR {k + 1}: {msg}")))
        s.finished.connect(lambda: self._on_finished(k, engine))

    def stop(self) -> None:
        for engine in self._engines:
            engine.request_stop()

    # -- external add-ons (rtl_433, dumpvdl2): they hold one receiver instead of the scanner -----------
    def run_addon(self, addon_id: str, dongle: Optional[int] = None) -> None:
        """Hands a receiver (``dongle``, else the one chosen for the add-on) to an external program. With one
        receiver the scanner pauses; with several only that receiver's engine does, the others take over its
        share of the scan plan and keep scanning - and the scanner can be started or stopped meanwhile. Several
        add-ons may run at once, each on its own receiver."""
        with self._start_lock:
            if addon_id in self.addons.running_ids:
                raise RuntimeError(i18n.tr("{name} is already running.").format(name=addon_id))
            devices = self._devices()
            if dongle is None:
                dongle = self.addons.receiver(addon_id, 0)
            if not 0 <= dongle < len(devices):
                dongle = 0      # the receiver that was chosen has been removed since
            if dongle in self._held:
                other = next((a for a, d in self._addon_dongles.items() if d == dongle), "")
                raise RuntimeError(i18n.tr("Receiver {n} is already used by {name} - stop it first.").format(n=dongle + 1, name=other))
            with self._lock:
                engines = list(self._engines)
            targets = engines if len(devices) == 1 else engines[dongle:dongle + 1]
            targets = [e for e in targets if e.is_alive() and not e._stop_evt.is_set()]
            for engine in targets:
                engine.request_stop()
            for engine in targets:
                self._join(engine, 8.0)
            if any(e.is_alive() for e in targets):
                raise RuntimeError(i18n.tr("Scanner is still stopping - try again"))
            self._resume = (bool(targets) and len(devices) == 1) or self._resume
            self._addon_dongles[addon_id] = dongle
            self._held.add(dongle)
            for e in engines:
                if e not in targets and e.is_alive():
                    e.request_rebuild()       # they take over what this receiver scanned
            try:
                self.addons.start_external(addon_id, asdict(self.config.device) if devices[dongle] is None else devices[dongle])
            except Exception:
                self._resume_scanning(addon_id)
                raise

    def stop_addon(self, addon_id: str = "") -> None:
        """Stops one add-on (or all). Stopping a scheduled one by hand keeps the schedule from starting it again
        until its time range has passed."""
        with self._start_lock:
            ids = [addon_id] if addon_id else list(self._addon_dongles)
            for i in ids:
                if i in self._sched_started or self._scheduled_receiver(i) is not None:
                    self._sched_suppressed[i] = self._schedule_key(i)
        self.addons.stop_external(addon_id)
        with self._start_lock:
            for i in ids or list(self._addon_dongles):
                self._resume_scanning(i)

    def _addon_ended(self, addon_id: str) -> None:
        """An external program ended (its own exit or a stop): give its receiver back, off the reader's thread."""
        def release() -> None:
            with self._start_lock:
                self._resume_scanning(addon_id)
        threading.Thread(target=release, daemon=True, name="addon-release").start()

    def _resume_scanning(self, addon_id: str) -> None:
        dongle = self._addon_dongles.pop(addon_id, None)
        self._sched_started.pop(addon_id, None)
        if dongle is None:
            return
        self._held.discard(dongle)
        devices = self._devices()
        if len(devices) == 1:
            resume, self._resume = self._resume, False
            if resume:
                self._start()
            return
        with self._lock:
            running = bool(self._engines)
        if running:
            self._restart_engine(dongle)
            for e in list(self._engines):
                if e.is_alive():
                    e.request_rebuild()       # they give the share back

    def _restart_engine(self, k: int) -> None:
        """Starts the engine of one receiver again while the others keep scanning."""
        with self._lock:
            if k >= len(self._engines):
                return
            old = self._engines[k]
        self._join(old, 8.0)
        devices = self._devices()
        engine = ScannerEngine(self.config, self.channels, self.blacklist, device=devices[k] if k < len(devices) else None,
                               share=(k, len(devices)), shared=self._shared)
        engine.addons = self.addons
        with self._lock:
            self._engines[k] = engine
            self._finished.discard(k)
            self._lives[k] = {**self._idle_live(), "state": "CALCULATE_NEXT_FREQ"}
        self._connect(k, engine, len(self._engines))
        engine.start()

    # -- the schedule starts and stops add-ons --------------------------------------------------------------
    def _plans(self, held: Optional[set] = None) -> list:
        n = len(self._devices())
        return schedule.assignments(
            self.config, n, self._held if held is None else held,
            addon_receiver=lambda a: self.addons.receiver(a, 0), addon_ok=self.addons.can_run)

    def _scheduled_receiver(self, addon_id: str) -> Optional[int]:
        """The receiver the schedule wants this add-on on right now, or None."""
        manual = {d for a, d in self._addon_dongles.items() if a not in self._sched_started}
        for k, p in enumerate(self._plans(manual)):
            if p.addon == addon_id:
                return k
        return None

    def _schedule_key(self, addon_id: str) -> tuple:
        manual = {d for a, d in self._addon_dongles.items() if a not in self._sched_started}
        return next((p.entries for p in self._plans(manual) if p.addon == addon_id), ())

    def _schedule_loop(self) -> None:
        while not self._bg_stop.wait(5.0):
            try:
                self.reconcile_schedule()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def reconcile_schedule(self) -> None:
        """Brings the running add-ons in line with the schedule (called every few seconds)."""
        addons = self.addons
        manual = {d for a, d in self._addon_dongles.items() if a not in self._sched_started}
        sched = self.config.schedule
        # What the entries ask for, whether or not it can run - failures are forgotten once the entry is over.
        wanted = {e.addon for i, e in enumerate(sched.entries) if sched.enabled and e.addon and i in schedule.active_indices(self.config)}
        addons.schedule_failed &= wanted
        for a in [a for a in self._sched_suppressed if a not in wanted]:
            del self._sched_suppressed[a]
        plans = schedule.assignments(self.config, len(self._devices()), manual,
                                     addon_receiver=lambda a: addons.receiver(a, 0), addon_ok=addons.can_run)
        want = {p.addon: (k, p.entries) for k, p in enumerate(plans) if p.addon}
        for a, k in list(self._sched_started.items()):
            if a not in want or want[a][0] != k:
                self.stop_addon_by_schedule(a)
        for a, (k, entries) in want.items():
            if a in addons.running_ids or self._sched_suppressed.get(a) == entries:
                continue
            try:
                self.run_addon(a, k)
                self._sched_started[a] = k
                self.log(i18n.tr("Schedule: {name} started on receiver {n}.").format(name=a, n=k + 1))
            except Exception as exc:  # noqa: BLE001 - shown once; the receiver is not held meanwhile
                addons.schedule_failed.add(a)
                self.log(i18n.tr("Schedule: could not start {name}: {error}").format(name=a, error=exc))

    def stop_addon_by_schedule(self, addon_id: str) -> None:
        self.addons.stop_external(addon_id)
        with self._start_lock:
            self._resume_scanning(addon_id)
        self.log(i18n.tr("Schedule: {name} stopped.").format(name=addon_id))

    def schedule_now(self) -> list[dict]:
        """What every receiver does right now, for the Schedule page."""
        devices = self._devices()
        plans = self._plans()
        names = [e.name or f"{e.start}-{e.end}" for e in self.config.schedule.entries]
        out = []
        for k in range(len(devices)):
            running = next((a for a, d in self._addon_dongles.items() if d == k), "")
            p = plans[k]
            out.append({"receiver": k, "addon": running, "by_schedule": running in self._sched_started,
                        "scanning": not running and bool(p.bands or p.memory), "memory": p.memory and not running,
                        "bands": [b.label for b in p.bands] if not running else [], "entries": [names[i] for i in p.entries if i < len(names)]})
        return out

    def _on_finished(self, k: int, engine: ScannerEngine) -> None:
        with self._lock:
            if k >= len(self._engines) or self._engines[k] is not engine:
                return
            self._finished.add(k)
            if len(self._finished) == len(self._engines):
                self._engines, self._lives = [], []
                self._spectrum = None

    def _target(self) -> ScannerEngine:
        """The engine a listener's command means: the one being heard."""
        engines = self._engines
        if not engines:
            raise RuntimeError(i18n.tr("Scanner is not running"))
        return engines[self._audible] if self._audible < len(engines) else engines[0]

    def command(self, name: str, payload: dict) -> None:
        if name == "solo":
            idx = payload.get("band")
            if idx is not None and not 0 <= int(idx) < len(self.config.bands):
                raise ValueError("No such band")
            self._solo_band = int(idx) if idx is not None else None
            band = self.config.bands[self._solo_band] if self._solo_band is not None else None
            for engine in self._engines:
                engine.set_solo_band(band)
            return
        engine = self._target()
        enabled = bool(payload.get("enabled"))
        if name == "skip":
            engine.request_skip()
        elif name == "blacklist":
            engine.request_skip_and_blacklist()
        elif name == "hold":
            if enabled:
                engine.set_hold(True)
            else:
                for e in self._engines:  # "Resume" whichever is held
                    e.set_hold(False)
        elif name == "manual_record":
            if enabled:
                engine.start_manual_recording()
            else:
                for e in self._engines:
                    e.stop_manual_recording()
        elif name == "raw_iq":
            if enabled:
                engine.start_raw_iq_recording()
            else:
                for e in self._engines:
                    e.stop_raw_iq_recording()
        else:
            raise ValueError("Unknown command")

    # -- engine signal handlers (engine threads) ------------------------------
    def _set_live(self, k: int, **values: Any) -> None:
        lost = False
        with self._lock:
            if k < len(self._lives):
                lost = values.get("state") == "RECONNECTING" and self._lives[k].get("state") != "RECONNECTING"
                self._lives[k].update(values)
        if lost:
            self.notifier.on_system("receiver_lost", i18n.tr("Receiver {n} lost its connection - reconnecting.").format(n=k + 1))

    def _notify_recording(self, path: str) -> None:
        from .playback.organizer import parse_filename
        parsed = parse_filename(Path(path).name)
        if parsed is None or not parsed.freq_khz:
            return
        names = self.freq_names()
        self.notifier.on_recording(parsed.freq_khz * 1000, names.get(parsed.freq_khz, ""), str(path))

    def _on_spectrum(self, k: int, freqs: Any, db: Any, trigger_dbfs: float, custom: bool) -> None:
        if k != self._audible:
            return  # the spectrum follows the dongle being heard
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

    def _on_antennas(self, k: int, ants: tuple[str, ...]) -> None:
        items = [str(a) for a in ants if a]
        self._set_live(k, antennas=items)
        if k == 0 and items:
            with self._lock:
                self._known_antennas = items

    def _on_blacklisted(self, freq_hz: int) -> None:
        entry = BlacklistEntry(
            freq_hz=int(freq_hz), margin_hz=self.config.default_blacklist_margin_hz,
            label="Auto-lockout (SKIP)", date_added=dt.date.today().isoformat(),
        )
        with self._lock:
            updated = self.blacklist + [entry]
            self.blacklist = updated
        storage.save_blacklist(updated)
        with self._lock:
            engines = list(self._engines)
        for engine in engines:
            engine.update_blacklist(updated)

    def _on_live_audio(self, k: int, mono: Any) -> None:
        with self._lock:
            # One dongle on the speaker at a time: the one heard keeps it while
            # its transmission lasts, then the next one to hear something takes over.
            owner = self._audible
            if k != owner and owner < len(self._lives) and self._lives[owner]["state"] in _ACTIVE:
                return
            self._audible = k
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

    def health(self) -> list[dict]:
        """Acquisition counters of every receiver that is open now (Settings -> Receiver health)."""
        with self._lock:
            engines = list(self._engines)
        return [{"receiver": k, **h} for k, engine in enumerate(engines) if (h := engine.health()) is not None]

    def probe(self) -> None:
        """Starts the receiver test - only while nothing holds the receiver."""
        if self.live()["running"] or self.addons.running_external:
            raise RuntimeError(i18n.tr("Stop the scanner first"))
        sdr_probe.start_in_background()

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
    def _merged_live_locked(self) -> dict[str, Any]:
        """One status for the UI out of every dongle's: the heard one's
        frequencies, level and gain first, then whatever the others have on air."""
        lives = self._lives
        if not lives:
            return self._idle_live()
        heard = lives[self._audible] if self._audible < len(lives) else lives[0]
        if len(lives) == 1:
            return dict(heard)
        others = [f for live in lives if live is not heard and live["state"] in _ACTIVE for f in live["frequencies"]]
        return {
            **heard,
            "state": min((live["state"] for live in lives), key=lambda s: _STATE_RANK.index(s) if s in _STATE_RANK else 99),
            "frequencies": heard["frequencies"] + others,
            **{key: any(live[key] for live in lives) for key in ("recording", "manual_recording", "raw_iq", "hold")},
            "dongles": [{"state": live["state"], "frequencies": live["frequencies"]} for live in lives],
        }

    def live(self, since_log: int = 0) -> dict:
        with self._lock:
            return {
                **self._merged_live_locked(),
                "running": bool(self._engines),
                "stopping": any(e._stop_evt.is_set() for e in self._engines),
                "solo_band": self._solo_band,
                "uncovered": self._uncovered(),
                "schedule_active": schedule.active_indices(self.config),
                "clock": time.strftime("%H:%M"),
                "spectrum": self._spectrum,
                "log": [e for e in self._log if e["id"] > since_log],
                "audio_rate": AUDIO_RATE,
                "addon": self.addons.running_external,
                "addons": [{"id": a, "receiver": d} for a, d in self._addon_dongles.items()],
                "notify_seq": self.notifier.seq,
                "addon_since": self.addons.started_at,
                "schedule_now": self.schedule_now() if self.config.schedule.enabled else [],
                "update": self.updates.available(),
            }

    def _uncovered(self) -> list[dict]:
        """Selected bands/channels outside every dongle's antenna range (antenna_range.py)."""
        if not antenna_range.is_limited(antenna_range.device_ranges(self.config)):
            return []
        bands, memory_on = schedule.union_selection(self._plans())
        return antenna_range.uncovered(self.config, self.channels, bands, memory_on)

    # -- notifications (notifier.py) ---------------------------------------------------------------------------
    def notify_state(self) -> dict:
        n = self.config.notify
        return {"enabled": n.enabled, "channels": mask(n.channels), "rules": n.rules, "recent": self.notifier.since(0)[-30:]}

    def notify_save(self, body: dict) -> dict:
        n = self.config.notify
        old = {c["id"]: c for c in n.channels}
        channels = [clean_channel(c, old.get(c.get("id"))) for c in body.get("channels", []) if isinstance(c, dict)][:20]
        ids = {c["id"] for c in channels}
        rules = [clean_rule(r, ids) for r in body.get("rules", []) if isinstance(r, dict)][:50]
        n.enabled, n.channels, n.rules = bool(body.get("enabled", n.enabled)), channels, rules
        self.config.save()
        return self.notify_state()

    def notify_test(self, channel_id: str, draft: Optional[dict] = None) -> str:
        old = {c["id"]: c for c in self.config.notify.channels}
        channel = clean_channel(draft, old.get(channel_id)) if draft else old.get(channel_id)
        if channel is None:
            raise ValueError("No such channel")
        return self.notifier.test(channel)

    def _enumerated_hardware(self, refresh: bool = False) -> tuple[list[dict], list[str]]:
        """Caches hardware enumeration so opening UI tabs while scanning does not
        re-probe SoapySDR/USB (slow, noisy ALSA/Avahi stderr, and busy-device errors).
        Refreshed automatically every 60 s while stopped, or on demand via refresh=True."""
        if not isinstance(self, Controller):
            return enumerate_devices(), sdr_native.usb_receivers_seen()
        now = time.monotonic()
        cached = self._devices_cache
        if cached is not None and not refresh and (self.running or now - cached[0] < 60.0):
            return cached[1], cached[2]
        devs = enumerate_devices()
        usb = sdr_native.usb_receivers_seen()
        self._devices_cache = (now, devs, usb)
        return devs, usb

    def settings(self, refresh_devices: bool = False) -> dict:
        devices = self.devices(refresh=refresh_devices)
        _, usb_seen = self._enumerated_hardware(refresh=False)
        return {
            "version": __version__,
            "config": asdict(self.config),
            "channels": [channel_dict(c) for c in self.channels],
            "blacklist": [asdict(b) for b in self.blacklist],
            "presets": [
                {**p.to_dict(), "user": military_presets.is_user_owned_preset(p.id)}
                for p in military_presets.get_all_presets()
            ],
            "devices": devices,
            "antennas": list(self._known_antennas),
            "languages": [{"code": c, "name": i18n.LANGUAGE_NAMES.get(c, c)} for c in i18n.available_languages()],
            "capabilities": {
                "ffmpeg": ffmpeg_available(),
                "addon_messages": bool(self.addons.hub.since(0, limit=1)),     # something was decoded: Messages is worth showing
                "transcription_installed": is_transcription_installed(),
                "digital": digital_voice.is_digital_installed(),
                "tetra": tetra_addon.is_tetra_installed(str(self.config.addons.paths.get("tetra", ""))),
                "platform": sys.platform,
                "soapy": HAVE_SOAPY,
                "native": {k: sdr_native.native_status()[k] for k in ("hackrf", "airspy")},
                "usb_seen": usb_seen,
                "autostart": autostart.is_enabled() if autostart.supported() else None,
            },
        }

    def devices(self, refresh: bool = False) -> list[dict]:
        """Every way to open a receiver, one entry each: the UI groups them by ``group`` (one physical
        receiver) and offers ``method`` as "how to access it". The first entry of a group is its default."""
        raw_devs, usb_seen = Controller._enumerated_hardware(self, refresh=refresh)
        out = [{"driver": "auto", "args": "", "label": i18n.tr("Automatic (first detected)"), "group": "auto", "method": ""}]
        for dev in raw_devs:
            out.append(self._device_entry(dev.get("driver", "auto"), {k: str(v) for k, v in dev.items()},
                                          dev.get("label") or dev.get("driver", "device")))
        # An RTL-SDR on another computer (rtl_tcp): nothing to detect - its address is typed on the Listen page and
        # kept in device_args, which this entry carries while it is the chosen one.
        out.append({"driver": RTLTCP_DRIVER, "args": self.config.device.device_args if self.config.device.driver == RTLTCP_DRIVER else "",
                    "label": i18n.tr("Network receiver (rtl_tcp)"), "family": RTLTCP_DRIVER, "method": device_method(RTLTCP_DRIVER),
                    "group": RTLTCP_DRIVER})
        # A busy receiver (the scanner has it open) can drop out of a fresh listing - the saved choice must not.
        saved = [(self.config.device.driver, self.config.device.device_args)] + \
                [(x.get("driver", ""), x.get("device_args", "")) for x in self.config.device.extra_devices]
        for driver, args in saved:
            if driver and driver not in ("auto", "simulated", RTLTCP_DRIVER) and not any(e["driver"] == driver and e["args"] == args for e in out):
                fields_ = dict(kv.split("=", 1) for kv in args.split(",") if "=" in kv)
                entry = self._device_entry(driver, fields_, fields_.get("label") or driver, args)
                # The same receiver listed with other details (busy, or another SoapySDR version): the saved
                # arguments are the ones in use, so the UI must find them - not a second copy of the entry.
                twin = next((e for e in out if e["driver"] == driver and e["group"] == entry["group"]), None)
                if twin is not None:
                    twin["args"] = args
                else:
                    out.append(entry)
        # Plugged in (USB ids) but nothing here can open it: still list it, so it is not "missing" - choosing
        # it fails with the exact thing to install (the Listen page shows the error while RECONNECTING).
        have = {e.get("family") for e in out}
        for family, name in (("hackrf", "HackRF"), ("airspy", "Airspy")):
            if family in usb_seen and family not in have:
                label = f"{name} (plugged in - needs libhackrf/SoapySDR)" if family == "hackrf" else f"{name} (plugged in - needs libairspy/SoapySDR)"
                entry = self._device_entry(f"{family}_native", {"label": label}, label)
                entry["missing"] = True
                out.append(entry)
        if self.config.debug_mode:
            out.append({"driver": "simulated", "args": "", "label": i18n.tr("Simulation (no hardware)"), "group": "simulated", "method": ""})
        return out

    @staticmethod
    def _device_entry(driver: str, dev: dict, label: str, args: str | None = None) -> dict:
        family = device_family(driver)
        ident = (dev.get("serial") or "").lower().lstrip("0") or (f"#{dev['index']}" if "index" in dev else label)
        entry = {
            "driver": driver,
            "args": args if args is not None else ",".join(f"{k}={v}" for k, v in dev.items()),
            "label": label, "family": family, "method": device_method(driver), "group": f"{family}:{ident}",
        }
        if family == "airspy":
            entry["default_rate_hz"] = 2_500_000  # the R2's slow mode; a Mini needs 3 MHz - the error says so
        return entry

    # -- edits -------------------------------------------------------------------
    def patch_config(self, patch: dict) -> None:
        old_language = self.config.language
        old_root = self.config.recording.output_dir
        for locked in ("access_token", "workers", "notify"):     # these have their own endpoints
            patch.pop(locked, None)
        if isinstance(patch.get("addons"), dict):
            for locked in ("paths", "extra_args"):               # a command line: only /api/addons/configure, which checks who asks
                patch["addons"].pop(locked, None)
        merge_patch(copy.deepcopy(self.config), patch)  # rejects a bad value before anything is applied
        merge_patch(self.config, patch)
        # An emptied folder field would otherwise mean "the current directory".
        self.config.recording.output_dir = self.config.recording.output_dir.strip() or DEFAULT_RECORDINGS_DIR
        self.config.save()
        if self.config.language != old_language:
            i18n.set_language(self.config.language)
        if "updates" in patch:
            self.updates.wake()      # switched on: check now, not in a minute
        if self.config.recording.output_dir != old_root:
            threading.Thread(target=self.library.rescan, daemon=True).start()
        if not set(patch) <= _READ_LIVE_KEYS:  # a slider drag must not rebuild the scan plan at every step
            self._rebuild()

    def set_channels(self, channels: list[Channel]) -> None:
        with self._lock:
            self.channels = channels
            engines = list(self._engines)
        storage.save_channels(channels)
        self.library.invalidate_search()
        for engine in engines:
            engine.update_channels(channels)
            engine.request_rebuild()

    def set_blacklist(self, blacklist: list[BlacklistEntry]) -> None:
        with self._lock:
            self.blacklist = blacklist
            engines = list(self._engines)
        storage.save_blacklist(blacklist)
        for engine in engines:
            engine.update_blacklist(blacklist)

    def _rebuild(self) -> None:
        if self._solo_band is not None and self._solo_band >= len(self.config.bands):
            self._solo_band = None
        for engine in self._engines:
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
        fresh.updates = self.config.updates      # an answered question stays answered
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
        if command not in ("install", "install_gpu", "uninstall", "uninstall_with_models", "restart", "upload_local", "detach"):
            raise ValueError("Unknown worker command")
        with self._lock:
            queued = self.worker_commands.setdefault(name, [])
            if command not in queued:  # a double click must not install twice
                queued.append(command)

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
