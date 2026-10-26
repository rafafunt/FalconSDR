"""The scanner: a background thread running the state machine
(CALCULATE_NEXT_FREQ -> TUNING -> SCANNING -> SIGNAL_DETECTED <-> LINGER) that
writes audio only for transmissions it actually stops on.

Wideband: the channels are grouped into as few capture windows as fit the
SDR's bandwidth (channelizer.plan_windows) and every channel in a window is
measured from one FFT per block. Several active channels in a window are
serviced at once, each in its own _Slot.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .agc import SoftwareAgc
from .audio_transcode import transcode_to_mp3
from .calibration_log import CalibrationLogger
from .edge_compensation import EdgeCompensation
from .channelizer import (
    USABLE_FRACTION, ScanWindow, channel_powers_dbfs, channel_powers_from_spectrum, extend_boundary_window,
    plan_windows, spectrum_cumsum, spectrum_snapshot,
)
from .config import AppConfig, CONFIG_DIR, RecordingConfig, VfoRangeConfig
from .dsp import Demodulator, NoiseReducer
from .i18n import tr
from . import schedule
from .iq_recorder import IQRecorder
from .models import BlacklistEntry, Channel, Modulation, ScannerState, resolve_ssb_sideband
from .recorder import Recorder
from .sdr_backend import AcquisitionThread, create_backend, open_real_backend
from .wav_metadata import append_metadata_chunk

AUDIO_RATE = 12500
CHUNK_SECONDS = 0.03
MAX_VFO_CHANNELS = 20_000

# Channels closer than this to an active WFM slot are its own sidebands
# (a broadcast station is ~200 kHz wide; same-area stations sit >=200 kHz apart).
_WFM_GUARD_HZ = 200_000

# Reflection detection (_detect_reflections): a strong channel can produce a
# correlated, 10-25 dB weaker copy on another channel of the same window
# (intermod/image/splatter) - measured on real recordings.
_REFLECTION_HISTORY_TICKS = 50  # ~1.5 s of level history per slot
# Both slots must have been active this long (~450 ms): two real transmissions
# can start in the same tick just because the scanner tuned onto both.
_REFLECTION_MIN_OVERLAP_TICKS = 15
# Pearson correlation of the two level histories - unrelated signals
# practically never reach it, a receiver artifact does.
_REFLECTION_CORR_THRESHOLD = 0.92
# Mean level gap, so two comparably strong real signals are never merged.
_REFLECTION_MIN_GAP_DB = 8.0
# The check is O(n^2) per tick: above this many slots it is skipped (gotcha 30).
_REFLECTION_MAX_SLOTS_CHECKED = 8
# FALCONSDR_DEBUG_LIVE_AUDIO=1: once a second, print how many SCANNING ticks
# overran CHUNK_SECONDS - a CPU deficit, not the scheduling starvation
# _maybe_boost_priority deals with.
_DEBUG_TIMING = os.environ.get("FALCONSDR_DEBUG_LIVE_AUDIO") == "1"
# A single backend.read() this slow is logged on its own line: real stalls
# were rare and discrete (up to 8 s), which the per-second summary washes out.
_READ_STALL_LOG_THRESHOLD_S = 0.5

_STARTUP_CHECKPOINT_PATH = CONFIG_DIR / "startup_checkpoints.log"


def _checkpoint(msg: str) -> None:
    """Best-effort breadcrumb trail through scanner startup, truncated at
    each run(). Kept for a still-unexplained case: the process vanished a few
    seconds after Start (no traceback, no OOM kill, only when launched from
    the .desktop file) - the file shows the last checkpoint reached."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with _STARTUP_CHECKPOINT_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.time():.3f} {msg}\n")
    except OSError:
        pass


# Constant 2 s retry, so replugging the dongle reconnects quickly.
RECONNECT_BACKOFF_S = [2]
# _next_window() skips fully blacklisted windows; bounded in case all are.
MAX_PICK_ATTEMPTS = 64
# Background-noise snapshots of one window (CalibrationLogger) at most this often.
_FLOOR_LOG_INTERVAL_S = 30.0


class _Lane:
    """One schedulable unit for the weighted scanner: either a whole band
    sweeping through its own windows in turn (``is_range=True``), or one
    (possibly merged) set of priority Memory List frequencies. Lanes with
    weight 0 don't get their own _Lane - they're pooled into the flat
    "default lane" instead (see ScannerEngine._rebuild_windows).
    """

    __slots__ = ("weight", "windows", "is_range", "index", "current", "just_wrapped")

    def __init__(self, weight: float, windows: list[ScanWindow], is_range: bool):
        self.weight = weight
        self.windows = windows
        self.is_range = is_range
        self.index = 0
        self.current = 0.0  # _WeightedScheduler's running credit for this lane
        self.just_wrapped = False

    def advance(self) -> ScanWindow | None:
        """Returns this lane's next window and moves its own internal
        rotation forward by one step, so consecutive turns for the same lane
        resume exactly where the previous one left off."""
        if not self.windows:
            self.just_wrapped = False
            return None
        window = self.windows[self.index]
        self.index += 1
        if self.index >= len(self.windows):
            self.index = 0
            self.just_wrapped = True
        else:
            self.just_wrapped = False
        return window


class _WeightedScheduler:
    """Smooth weighted round robin over lanes with weight > 0 (same
    algorithm nginx uses for upstream weights): a lane with twice the weight
    of another gets picked twice as often, interleaved evenly rather than in
    bursts - e.g. weights (1, 1) alternate A,B,A,B,... and (2, 1) come out
    roughly A,B,A,A,B,A,...
    """

    def __init__(self, lanes: list[_Lane]):
        self.lanes = lanes
        self.total_weight = sum(l.weight for l in lanes)

    def pick(self) -> _Lane:
        for lane in self.lanes:
            lane.current += lane.weight
        best = max(self.lanes, key=lambda l: l.current)
        best.current -= self.total_weight
        return best


def _raster(band_lo: int, start: float, end: float, step_hz: int) -> list[int]:
    """The band's channel raster (band_lo + k * step) inside [start, end].
    Always anchored at the band's own start, never at a window edge (that
    once put channels at arbitrary offsets - e.g. 131.3996 instead of
    131.400), and step 8333/8334 means the airband's 8.33 kHz raster,
    i.e. exactly 25000/3 Hz (an integer 8333 drifted 416 Hz by 131.4 MHz)."""
    step = 25000 / 3 if step_hz in (8333, 8334) else float(max(1, step_hz))
    k0 = max(0, math.ceil((start - band_lo) / step - 1e-9))
    k1 = math.floor((end - band_lo) / step + 1e-9)
    return [int(round(band_lo + k * step)) for k in range(k0, k1 + 1)]


def _band_grid(vr: VfoRangeConfig) -> tuple[list[int], Modulation]:
    """The full list of channel frequencies a band/range sweeps at its own
    step, plus the single modulation it demodulates all of them with."""
    lo, hi = min(vr.start_hz, vr.stop_hz), max(vr.start_hz, vr.stop_hz)
    band_mod = Modulation(vr.modulation) if vr.modulation in Modulation._value2member_map_ else Modulation.AM
    return _raster(lo, lo, hi, vr.step_hz) or [lo], band_mod


def _overlapping_band(freqs: list[int], bands: list[VfoRangeConfig]) -> VfoRangeConfig | None:
    """The first enabled band (weighted or not) whose range contains any of
    ``freqs`` - used to decide whether a priority Memory List frequency gets
    the "wide anchored window" treatment (rule: a weighted channel inside an
    actively-scanned band should keep covering its neighbours too)."""
    for vr in bands:
        lo, hi = min(vr.start_hz, vr.stop_hz), max(vr.start_hz, vr.stop_hz)
        if any(lo <= f <= hi for f in freqs):
            return vr
    return None


def _wide_anchor_windows(freqs: list[int], band: VfoRangeConfig, sample_rate: float) -> list[ScanWindow]:
    """Two full-width capture windows for a priority frequency (or small
    merged group of them) that lies inside ``band``: one anchoring the group
    near the left edge of the capture, one near the right edge, each filled
    out with that band's own grid so a priority "visit" doubles as an
    ordinary wide sweep of the surrounding channels (this is what lets
    detections on a neighbouring frequency still work normally, and lets the
    band's own rotation skip nothing - it just doesn't need to dwell here as
    often). Alternating which edge is anchored on successive visits sweeps
    fresh neighbouring spectrum each time instead of always re-covering the
    same span. Returns a single window if both edges end up identical (band
    narrower than one capture)."""
    capture_bw = sample_rate * USABLE_FRACTION
    lo_band, hi_band = min(band.start_hz, band.stop_hz), max(band.start_hz, band.stop_hz)
    anchor_lo, anchor_hi = min(freqs), max(freqs)

    def _variant(edge: str) -> ScanWindow:
        if edge == "left":
            win_start = max(lo_band, anchor_lo - capture_bw * 0.1)
            win_end = min(hi_band, win_start + capture_bw)
            win_start = max(lo_band, win_end - capture_bw)
        else:
            win_end = min(hi_band, anchor_hi + capture_bw * 0.1)
            win_start = max(lo_band, win_end - capture_bw)
            win_end = min(hi_band, win_start + capture_bw)
        grid = _raster(lo_band, win_start, win_end, band.step_hz)
        for f in freqs:
            if f not in grid:
                grid.append(f)
        grid.sort()
        center = (grid[0] + grid[-1]) // 2
        return ScanWindow(center_hz=center, freqs=grid)

    left, right = _variant("left"), _variant("right")
    return [left] if left.freqs == right.freqs else [left, right]


class _SpectrumWorker:
    """Runs spectrum_snapshot() - a large FFT over the whole IQ block - on its
    own thread, so a slow frame never delays live audio. Keeps only the
    latest job: a dropped spectrum frame is invisible, dropped audio isn't.
    """

    def __init__(self, signals: "ScannerSignals"):
        self._signals = signals
        self._lock = threading.Lock()
        self._pending: tuple | None = None
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(
        self, iq: np.ndarray, sample_rate: float, center_hz: float, trigger_dbfs: float, custom: bool = False,
    ) -> None:
        with self._lock:
            self._pending = (iq, sample_rate, center_hz, trigger_dbfs, custom)
        self._wake.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._stop_evt.is_set():
                return
            with self._lock:
                job = self._pending
                self._pending = None
            if job is None:
                continue
            iq, sample_rate, center_hz, trigger_dbfs, custom = job
            try:
                freqs, db = spectrum_snapshot(iq, sample_rate, center_hz)
                self._signals.spectrum_ready.emit(freqs, db, trigger_dbfs, custom)
            except Exception:
                pass


class _Slot:
    """One transmission being demodulated/recorded. Each slot owns its own
    Demodulator/NoiseReducer/Recorder, so simultaneous channels never share
    filter state. ``linger_deadline`` is None while the level is above the
    trigger, else the monotonic time the slot closes unless it comes back."""

    def __init__(
        self, freq_hz: int, name: str, modulation: Modulation, offset_hz: float,
        demod: Demodulator, noise_reducer: NoiseReducer, recorder: Recorder,
    ):
        self.freq_hz = freq_hz
        self.name = name
        self.modulation = modulation
        self.offset_hz = offset_hz
        self.demod = demod
        self.noise_reducer = noise_reducer
        self.recorder = recorder
        self.linger_deadline: float | None = None
        # Previous block was squelched: the noise reducer is reset when audio resumes.
        self.was_squelched = False
        # WAV metadata (wav_metadata.py): filled in _open_slot(), completed in _close_slot().
        self.meta: dict = {}
        self.opened_at = time.monotonic()
        # Window it was opened from - selects the band's own overrides, if any.
        self.window: ScanWindow | None = None
        # Per-tick levels for _detect_reflections(); every slot appends in the
        # same tick, so the histories are time-aligned without timestamps.
        self.level_history: deque[float] = deque(maxlen=_REFLECTION_HISTORY_TICKS)


class Signal:
    """Plain synchronous callbacks (gotcha 36): handlers run on the
    emitting thread (engine loop, slot pool, spectrum worker), so every
    handler must be thread-safe and quick. A failing handler is logged,
    never allowed to kill the engine thread."""

    def __init__(self) -> None:
        self._handlers: list = []

    def connect(self, handler) -> None:
        self._handlers.append(handler)

    def emit(self, *args) -> None:
        for handler in list(self._handlers):
            try:
                handler(*args)
            except Exception:  # noqa: BLE001
                traceback.print_exc()


class ScannerSignals:
    def __init__(self) -> None:
        self.state_changed = Signal()      # str
        self.level_changed = Signal()      # float
        # list[tuple[freq_hz, name]] - one entry per currently active channel,
        # primary (the one actually audible live) first, or a single placeholder
        # entry for the tuned window while nothing's active yet.
        self.freq_changed = Signal()
        self.recording_changed = Signal()  # (active, path)
        self.recording_finished = Signal()  # path of a just-closed per-transmission file
        self.manual_recording_changed = Signal()  # (active, path)
        self.hold_changed = Signal()       # bool
        # (active, path); never goes through recording_finished - not audio.
        self.raw_iq_recording_changed = Signal()
        self.blacklisted = Signal()        # freq_hz
        # freqs_hz, db, trigger_dbfs, custom (True = this window's band has its
        # own "advanced" overrides - drawn in an accent color)
        self.spectrum_ready = Signal()
        # Mono float32 audio (AUDIO_RATE) of the primary slot - what the
        # browser's live player streams (see controller.Controller).
        self.live_audio_ready = Signal()
        # Measured seconds since this window was last tuned (from the second visit on).
        self.cycle_time_changed = Signal()
        self.gain_range_changed = Signal()  # (lo_db, hi_db)
        self.log = Signal()                # str
        self.finished = Signal()


class ScannerEngine(threading.Thread):
    def __init__(self, config: AppConfig, channels: list[Channel], blacklist: list[BlacklistEntry]):
        super().__init__(daemon=True)
        self.signals = ScannerSignals()
        self.config = config
        self.channels = channels
        self.blacklist = blacklist
        # Vectorized mirror of self.blacklist - see _rebuild_blacklist_arrays().
        self._bl_freqs = np.empty(0, dtype=np.float64)
        self._bl_margins = np.empty(0, dtype=np.float64)
        self._rebuild_blacklist_arrays()

        self._stop_evt = threading.Event()
        self._skip_evt = threading.Event()          # "Skip" - move on now, frequency stays scannable
        self._skip_lockout_evt = threading.Event()   # "Skip + Blacklist" - move on now, permanently blacklist it
        self._hold = False  # "Hold" - stay on the current window; a plain bool, read once per tick

        self._backend = None
        self._spectrum_worker: _SpectrumWorker | None = None
        self._last_spectrum_submit_at = 0.0
        self._edge_compensation: EdgeCompensation | None = None
        self._logged_checkpoints: set[str] = set()  # see _checkpoint_once()

        # Guards the scheduling state below: _rebuild_windows() replaces it
        # from the API thread while _next_window() reads it on this thread.
        self._lock = threading.Lock()
        # Listen page's "scan only this band" (set_solo_band).
        self._solo_band: VfoRangeConfig | None = None
        # schedule.signature() the windows were last built for, checked every few seconds.
        self._schedule_sig: tuple | None = None
        self._schedule_logged: tuple | None = None  # last state told to the Activity feed
        self._schedule_check_at = 0.0
        self._priority_lanes: list[_Lane] = []
        self._default_lane = _Lane(weight=0.0, windows=[], is_range=False)
        self._scheduler: _WeightedScheduler | None = None
        self._macro_cycle_counter = 0.0
        self._default_due = False
        self._channel_bw_map: dict[int, float] = {}
        self._channel_name_map: dict[int, str] = {}
        self._channel_name_by_khz: dict[int, str] = {}  # fallback lookup - see _rebuild_windows
        self._channel_mod_map: dict[int, Modulation] = {}
        self._channel_weight_map: dict[int, float] = {}

        self._cur_window: ScanWindow | None = None
        # center_hz -> monotonic time of the last tune, for cycle_time_changed.
        self._window_last_visit: dict[int, float] = {}
        # Lets _main_loop skip retuning to the window it's already on.
        self._last_tuned_center_hz: float | None = None
        self._slots: dict[int, _Slot] = {}
        self._primary_freq: int | None = None  # which slot's audio is on the live speaker
        self._primary_weight = -1.0

        self._applied_gain_mode = ""
        self._applied_gain_db = None
        # Only while the effective gain mode is "sw_auto" (agc.py).
        self._software_agc: SoftwareAgc | None = None
        self._had_real_hardware = False

        # Manual recording: used from the API thread, this loop and the slot
        # pool, hence its own lock.
        self._manual_recorder: Recorder | None = None
        self._manual_recorder_lock = threading.Lock()
        # First frequency heard during a manual recording; the file is renamed
        # to it when the recording stops (gotcha 13).
        self._manual_recording_first_freq: int | None = None

        self._iq_recorder: IQRecorder | None = None
        self._iq_recorder_lock = threading.Lock()

        # Parallel per-slot demod/noise reduction/writes (gotcha 12) - measured
        # 3-4.6x faster, resample_poly/FFT release the GIL.
        self._slot_pool = ThreadPoolExecutor(
            max_workers=min(32, (os.cpu_count() or 4) + 4), thread_name_prefix="slot-demod",
        )

        self._calibration_log = CalibrationLogger()
        self._last_floor_log: dict[int, float] = {}
        # freq_hz -> monotonic time of the last "recording limit reached" log line
        self._capacity_logged: dict[int, float] = {}
        # center_hz -> boost state (_register_recency_boost); this thread only.
        self._recency_boosts: dict[int, dict] = {}

        # FALCONSDR_DEBUG_LIVE_AUDIO-gated timing stats - see _DEBUG_TIMING.
        self._tick_n = 0
        self._tick_over_budget = 0
        self._tick_max_s = 0.0
        self._tick_report_at = 0.0
        self._read_n = 0
        self._read_over_budget = 0
        self._read_max_s = 0.0

        self._last_emitted_state: ScannerState | None = None

    # -- public control (safe to call from other threads) -----------
    def request_stop(self) -> None:
        self._stop_evt.set()

    def request_skip(self) -> None:
        """Leave the current transmission(s) now, without blacklisting."""
        self._skip_evt.set()

    def request_skip_and_blacklist(self) -> None:
        """request_skip() that also blacklists every active frequency. No
        confirmation on purpose: it must be fast, and undoing it is one click."""
        self._skip_lockout_evt.set()

    def set_hold(self, enabled: bool) -> None:
        """Stay on the window being scanned now: its channels keep opening,
        recording and closing as usual, the scanner just doesn't move on."""
        self._hold = bool(enabled)
        self.signals.hold_changed.emit(self._hold)

    # -- manual continuous recording (gotcha 13) -------------------------
    # Independent of auto-recording: one continuous file for as long as it's
    # on, silence included and never trimmed. Fed with the primary slot's
    # audio (_play_and_record_slot) and with silence on idle ticks.
    def start_manual_recording(self) -> None:
        with self._manual_recorder_lock:
            if self._manual_recorder is not None:
                return
            manual_config = RecordingConfig(enabled=True, output_dir=self.config.recording.output_dir)
            recorder = Recorder(manual_config, AUDIO_RATE, single_file=True)
            recorder.start_session()
            self._manual_recorder = recorder
            self._manual_recording_first_freq = None
        self.signals.manual_recording_changed.emit(True, recorder.current_path)

    def stop_manual_recording(self) -> None:
        with self._manual_recorder_lock:
            recorder = self._manual_recorder
            if recorder is None:
                return
            self._manual_recorder = None
            first_freq = self._manual_recording_first_freq
            self._manual_recording_first_freq = None
        path = recorder.current_path
        recorder.stop_session()
        if path and first_freq is not None:
            renamed = Recorder.rename_session_file_with_frequency(Path(path), first_freq)
            if renamed is not None:
                path = str(renamed)
        self.signals.manual_recording_changed.emit(False, "")
        if path:
            self._finish_recording(Path(path))

    def is_manual_recording(self) -> bool:
        with self._manual_recorder_lock:
            return self._manual_recorder is not None

    def open_recording_paths(self) -> set[Path]:
        """Audio files still being written - the library must not list (and
        so never sync, mirror or delete) these yet. Called from other threads."""
        recorders = [slot.recorder for slot in list(self._slots.values())]
        with self._manual_recorder_lock:
            if self._manual_recorder is not None:
                recorders.append(self._manual_recorder)
        return {Path(r.current_path) for r in recorders if r.current_path}

    def _feed_manual_recording(self, audio: np.ndarray, freq_hz: int | None = None) -> None:
        """Writes ``audio`` to the manual recording, if one is running.
        ``freq_hz`` (real audio only) names the file on stop."""
        with self._manual_recorder_lock:
            recorder = self._manual_recorder
            if recorder is not None and freq_hz is not None and self._manual_recording_first_freq is None:
                self._manual_recording_first_freq = freq_hz
        if recorder is not None:
            recorder.write_audio(audio, real=True)

    # -- raw wideband IQ recording (gotcha 15) ---------------------------
    # Fed only by SCANNING reads, never by TUNING (the tuner is settling).
    def start_raw_iq_recording(self) -> None:
        with self._iq_recorder_lock:
            if self._iq_recorder is not None:
                return
            recorder = IQRecorder(self.config.recording.output_dir, self.config.device.sample_rate_hz)
            self._iq_recorder = recorder
        self.signals.raw_iq_recording_changed.emit(True, recorder.current_path)

    def stop_raw_iq_recording(self) -> None:
        with self._iq_recorder_lock:
            recorder = self._iq_recorder
            if recorder is None:
                return
            self._iq_recorder = None
        recorder.close()
        self.signals.raw_iq_recording_changed.emit(False, "")

    def is_recording_raw_iq(self) -> bool:
        with self._iq_recorder_lock:
            return self._iq_recorder is not None

    def _feed_raw_iq(self, iq: np.ndarray, center_freq_hz: float) -> None:
        with self._iq_recorder_lock:
            recorder = self._iq_recorder
        if recorder is not None:
            recorder.write(iq, center_freq_hz)

    def update_channels(self, channels: list[Channel]) -> None:
        self.channels = channels
        self._rebuild_windows()

    def update_blacklist(self, blacklist: list[BlacklistEntry]) -> None:
        self.blacklist = blacklist
        self._rebuild_blacklist_arrays()

    def _rebuild_blacklist_arrays(self) -> None:
        """Numpy copy of self.blacklist for _blacklist_mask(), rebuilt only
        when the blacklist changes. The per-tick check compares every window
        frequency against a blacklist that grows with each "Skip + blacklist";
        done in Python it slowed the loop down the longer the app ran.
        """
        if self.blacklist:
            self._bl_freqs = np.array([e.freq_hz for e in self.blacklist], dtype=np.float64)
            self._bl_margins = np.array([e.margin_hz for e in self.blacklist], dtype=np.float64)
        else:
            self._bl_freqs = np.empty(0, dtype=np.float64)
            self._bl_margins = np.empty(0, dtype=np.float64)

    def _blacklist_mask(self, freqs: list[int]) -> np.ndarray:
        """Vectorized [self._is_blacklisted(f) for f in freqs]."""
        if not freqs or self._bl_freqs.size == 0:
            return np.zeros(len(freqs), dtype=bool)
        arr = np.asarray(freqs, dtype=np.float64)[:, None]
        return np.any(np.abs(arr - self._bl_freqs[None, :]) <= self._bl_margins[None, :], axis=1)

    def request_rebuild(self) -> None:
        """Called after any change to what gets scanned, so a running scan
        picks it up without Stop/Start."""
        self._rebuild_windows()

    def set_solo_band(self, band: VfoRangeConfig | None) -> None:
        """Scan only this band (None = everything again). Safe to call
        before the thread starts."""
        self._solo_band = band
        self._rebuild_windows()

    # -- thread body ---------------------------------------------------
    def _maybe_boost_priority(self) -> None:
        """Best-effort SCHED_FIFO for this thread (like JACK/PipeWire), so
        desktop load doesn't cause live-audio dropouts. Linux only; needs root
        or an rtprio limit (often granted to the "audio" group), silently
        skipped otherwise."""
        if not hasattr(os, "sched_setscheduler"):
            return  # not Linux
        try:
            tid = threading.get_native_id()
            os.sched_setscheduler(tid, os.SCHED_FIFO, os.sched_param(10))
        except (PermissionError, OSError):
            pass

    def _checkpoint_once(self, msg: str) -> None:
        """_checkpoint() once per run() - for points inside the per-tick loop."""
        if msg not in self._logged_checkpoints:
            self._logged_checkpoints.add(msg)
            _checkpoint(msg)

    def run(self) -> None:
        self._logged_checkpoints.clear()
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _STARTUP_CHECKPOINT_PATH.write_text("", encoding="utf-8")  # fresh trail for this attempt - see _checkpoint()
        except OSError:
            pass
        _checkpoint("run() start")
        self._maybe_boost_priority()
        _checkpoint("priority boost attempted")
        self._setup_static()
        _checkpoint("_setup_static() done")
        connected = self._open_backend_with_retry(allow_simulated_fallback=True)
        _checkpoint(f"initial backend open done, connected={connected}")
        self._had_real_hardware = connected and self._backend.label != "simulated"

        while connected and not self._stop_evt.is_set():
            try:
                self._main_loop()
            except Exception as exc:
                # A bug in the loop lands here too, not only a lost device - keep its traceback.
                traceback.print_exc()
                error = str(exc) if isinstance(exc, (RuntimeError, OSError)) else f"{type(exc).__name__}: {exc}"
                self.signals.log.emit(tr("SDR connection lost: {error}").format(error=error))
                for freq_hz in list(self._slots.keys()):
                    self._close_slot(freq_hz, reason="connection_lost")
                if self._backend is not None:
                    self._backend.close()
                    self._backend = None
                # Once real hardware has been proven present, never silently
                # drop back to fake data on reconnect - keep insisting on the
                # real device until it's actually back (or Stop is pressed).
                connected = self._open_backend_with_retry(allow_simulated_fallback=not self._had_real_hardware)
                if connected and self._backend.label != "simulated":
                    self._had_real_hardware = True

        # Stop during (re)connecting: _main_loop() never ran to emit IDLE.
        self._emit_state(ScannerState.IDLE)
        self._teardown()
        self.signals.finished.emit()

    def _setup_static(self) -> None:
        """Everything that doesn't depend on the SDR hardware being present."""
        self._spectrum_worker = _SpectrumWorker(self.signals)
        _checkpoint("_SpectrumWorker thread started")
        # Loaded once per start - the tuner's roll-off doesn't change.
        self._edge_compensation = EdgeCompensation(self.config.device.gain_mode)
        _checkpoint(f"EdgeCompensation loaded, buckets={self._edge_compensation.bucket_count}")
        if self._edge_compensation.has_data:
            self.signals.log.emit(
                tr("Edge roll-off compensation: active ({count} data buckets from listening history).").format(
                    count=self._edge_compensation.bucket_count,
                )
            )
        self._rebuild_windows()
        _checkpoint("_rebuild_windows() done")

    def _open_backend(self, allow_simulated_fallback: bool) -> None:
        dev = self.config.device
        _checkpoint(f"_open_backend: constructing (driver={dev.driver!r})")
        if allow_simulated_fallback or dev.driver == "simulated":
            raw_backend = create_backend(
                dev.driver, dev.device_args, dev.sample_rate_hz, dev.gain_mode, dev.gain_db,
                debug_mode=self.config.debug_mode,
            )
        else:
            # Reconnect after real hardware was seen: keep failing, never fall
            # back to simulation (gotcha 8/10).
            raw_backend = open_real_backend(dev.driver, dev.device_args, dev.sample_rate_hz, dev.gain_mode, dev.gain_db)
        # Hardware reads happen on their own thread (gotcha 11), so nothing in
        # this loop can delay them; it proxies the backend's interface.
        self._backend = AcquisitionThread(raw_backend, self._chunk_size(), debug=self.config.debug_mode)
        self._dropped_reported, self._dropped_reported_at = 0, 0.0
        _checkpoint(f"_open_backend: constructed (label={self._backend.label})")
        self._applied_gain_mode, self._applied_gain_db = dev.gain_mode, dev.gain_db
        self.signals.gain_range_changed.emit(*self._backend.gain_range_db)
        if self._backend.label == "simulated" and dev.driver != "simulated":
            self.signals.log.emit(tr("SoapySDR not found/configured - running in SIMULATION mode."))
        # Whether one tick's worth of samples fits in a single readStream() call.
        mtu = getattr(self._backend, "stream_mtu", None)
        if mtu is not None:
            chunk = self._chunk_size()
            fits = tr("fits in one readStream() call") if mtu >= chunk else tr("STILL needs multiple calls per tick")
            self.signals.log.emit(
                tr("SDR stream MTU: {mtu} samples/call (tick = {chunk} samples - {fits}).").format(
                    mtu=mtu, chunk=chunk, fits=fits,
                )
            )

    def _open_backend_with_retry(self, allow_simulated_fallback: bool) -> bool:
        """Keep (re)trying to open the SDR until it works or Stop is pressed.
        Returns False only on Stop."""
        attempt = 0
        while True:
            try:
                self._open_backend(allow_simulated_fallback)
                if attempt > 0:
                    self.signals.log.emit(tr("Reconnected to the SDR - resuming scanning."))
                return True
            except Exception as exc:
                if self._stop_evt.is_set():
                    return False
                attempt += 1
                backoff = RECONNECT_BACKOFF_S[min(attempt - 1, len(RECONNECT_BACKOFF_S) - 1)]
                self.signals.log.emit(
                    tr("Failed to connect to the SDR ({error}) - retrying in {backoff}s (attempt {attempt})...").format(
                        error=exc, backoff=backoff, attempt=attempt,
                    )
                )
                self._emit_state(ScannerState.RECONNECTING)
                deadline = time.monotonic() + backoff
                while time.monotonic() < deadline:
                    if self._stop_evt.is_set():
                        return False
                    time.sleep(0.1)

    def _report_dropped_chunks(self) -> None:
        # Lost IQ = gaps in live audio and recordings - say so outside debug
        # mode too (gotcha 30: this counter is the only place that sees it).
        dropped = self._backend.chunks_dropped
        now = time.monotonic()
        if dropped > self._dropped_reported and now - self._dropped_reported_at >= 30.0:
            self.signals.log.emit(
                tr("This computer can't keep up with the SDR - {n} blocks of radio data lost (gaps in audio and recordings).")
                .format(n=dropped - self._dropped_reported)
            )
            self._dropped_reported, self._dropped_reported_at = dropped, now

    def _teardown(self) -> None:
        # Manual and raw IQ recordings can't outlive the engine that feeds them.
        self.stop_manual_recording()
        self.stop_raw_iq_recording()
        for freq_hz in list(self._slots.keys()):
            self._close_slot(freq_hz, reason="stopped")
        if self._spectrum_worker is not None:
            self._spectrum_worker.stop()
        if self._backend is not None:
            self._backend.close()
        # Wait for in-flight writes/transcodes before a quick Stop/Start.
        self._slot_pool.shutdown(wait=True)

    def _rebuild_windows(self) -> None:
        """Rebuilds everything the weighted scanner needs to pick its next
        window.

        Bands and Memory List channels with weight 0 (the default) are
        pooled into one flat "default lane", scanned round-robin. Anything with
        weight > 0 becomes its own _Lane - either a "range lane" (a band,
        sweeping through its own windows turn by turn) or a "point lane" (one
        or more nearby weighted Memory List frequencies captured together in
        one window). Lanes compete via _WeightedScheduler, proportionally to
        their weight, so e.g. a band and a channel both weighted 1 alternate
        turns 50/50 regardless of how many windows the band itself has.

        The default lane gets a turn whenever a range-lane completes a full
        lap over its own windows (or, if priority lanes exist but none of
        them are range-lanes, once every full weighted-scheduler cycle) -
        that's what makes weight-0 items "wait their turn" and then get
        visited promptly once the weighted stuff has had a full sweep.
        """
        sample_rate = self.config.device.sample_rate_hz
        mem_bw = float(self.config.memory_channel_bandwidth_hz)

        if self._solo_band is not None:
            # Solo band (set_solo_band): nothing else competes for airtime.
            channels = []
            enabled_channels: list[Channel] = []
            weighted_channels: list[Channel] = []
            plain_channels: list[Channel] = []
            enabled_bands = [self._solo_band] if self._solo_band.enabled else []
        else:
            enabled_bands, memory_on = schedule.scan_selection(self.config)
            channels = list(self.channels) if memory_on else []
            enabled_channels = [c for c in channels if c.enabled]
            weighted_channels = [c for c in enabled_channels if c.weight > 0]
            plain_channels = [c for c in enabled_channels if c.weight <= 0]
        self._schedule_sig = schedule.signature(self.config)

        weighted_bands = [b for b in enabled_bands if b.weight > 0]
        plain_bands = [b for b in enabled_bands if b.weight <= 0]

        bw_map: dict[int, float] = {}
        name_map: dict[int, str] = {}
        mod_map: dict[int, Modulation] = {}
        # Only used to decide which simultaneously-active channel gets the
        # live speaker when more than one is on at once - see _open_slot().
        weight_map: dict[int, float] = {}

        def _claim(freq: int, bw: float, mod: Modulation, name: str = "", weight: float = 0.0) -> None:
            bw_map[freq] = bw
            # SSB means "auto" to the user - resolved to USB/LSB per frequency.
            mod_map[freq] = resolve_ssb_sideband(mod, freq)
            weight_map[freq] = weight
            if name:
                name_map[freq] = name

        def _extend_edges(windows: list[ScanWindow], step_hz: float, band_freqs: set[int] | None = None) -> None:
            """Adds probe frequencies just past the lowest/highest window's
            edge, so a signal right at a band's boundary isn't lost in the
            unwatched margin (channelizer.extend_boundary_window - no extra
            retune). Probes inherit the edge channel's modulation/weight.

            ``band_freqs`` limits this to edges of a real swept band: a lone
            channel's "edge" is the channel itself, and extending it
            fabricated dozens of fake detections (gotcha 31)."""
            if not windows:
                return
            lowest = min(windows, key=lambda w: min(w.freqs))
            highest = max(windows, key=lambda w: max(w.freqs))
            for window, direction in ((lowest, "below"), (highest, "above")):
                edge_freq = min(window.freqs) if direction == "below" else max(window.freqs)
                if band_freqs is not None and edge_freq not in band_freqs:
                    continue
                mod = mod_map.get(edge_freq, Modulation.AM)
                weight = weight_map.get(edge_freq, 0.0)
                added = False
                for f in extend_boundary_window(window, sample_rate, step_hz, direction):
                    if f not in bw_map:
                        _claim(f, step_hz, mod, weight=weight)
                        window.freqs.append(f)
                        added = True
                if added:
                    window.freqs.sort()

        priority_lanes: list[_Lane] = []

        # -- priority point-lanes: weighted Memory List channels; nearby ones
        # share one window and the higher weight. ---------------------------
        if weighted_channels:
            weight_by_freq = {c.freq_hz: c.weight for c in weighted_channels}
            mod_by_freq = {c.freq_hz: c.modulation for c in weighted_channels}
            name_by_freq = {c.freq_hz: c.name for c in weighted_channels}
            groups = plan_windows([c.freq_hz for c in weighted_channels], sample_rate, mem_bw)
            for group in groups:
                lane_weight = max(weight_by_freq[f] for f in group.freqs)
                owning_band = _overlapping_band(group.freqs, enabled_bands)
                if owning_band is not None:
                    # Inside a scanned band: go wide, so the visit also covers its neighbours.
                    windows = _wide_anchor_windows(group.freqs, owning_band, sample_rate)
                    band_mod = (Modulation(owning_band.modulation)
                                if owning_band.modulation in Modulation._value2member_map_ else Modulation.AM)
                    for w in windows:
                        for f in w.freqs:
                            if f in mod_by_freq:
                                _claim(f, mem_bw, mod_by_freq[f], name_by_freq.get(f, ""), weight_by_freq[f])
                            elif f not in bw_map:
                                _claim(f, float(max(1, owning_band.step_hz)), band_mod, weight=owning_band.weight)
                else:
                    windows = [group]
                    for f in group.freqs:
                        _claim(f, mem_bw, mod_by_freq.get(f, Modulation.AM), name_by_freq.get(f, ""),
                               weight_by_freq.get(f, 0.0))
                priority_lanes.append(_Lane(weight=lane_weight, windows=windows, is_range=False))

        # -- priority range-lanes: weighted bands, each sweeping its own
        # grid in its own internal rotation across turns. -------------------
        for vr in weighted_bands:
            freqs, band_mod = _band_grid(vr)
            if len(freqs) > MAX_VFO_CHANNELS:
                keep_every = -(-len(freqs) // MAX_VFO_CHANNELS)
                freqs = freqs[::keep_every]
                self.signals.log.emit(
                    tr("Too many channels in band \"{label}\" ({count}) - thinned every {every}.").format(
                        label=vr.label, count=len(freqs) * keep_every, every=keep_every,
                    )
                )
            for f in freqs:
                if f not in bw_map:
                    _claim(f, float(max(1, vr.step_hz)), band_mod, weight=vr.weight)
            windows = plan_windows(freqs, sample_rate, max(1, vr.step_hz))
            # start == stop ("Single frequency" on Listen) is a point, not a
            # range - no edge to extend (gotcha 31).
            if vr.start_hz != vr.stop_hz:
                _extend_edges(windows, float(max(1, vr.step_hz)))
            for w in windows:
                w.band = vr  # unambiguous: this whole lane is one band's own grid
            priority_lanes.append(_Lane(weight=vr.weight, windows=windows, is_range=True))

        # -- default lane: everything with weight 0, pooled together. -------
        default_freqs: list[int] = []
        # A pooled window gets a band's overrides only if all its frequencies
        # come from that band; a window spanning two bands uses global settings.
        freq_to_band: dict[int, VfoRangeConfig] = {}
        # Real swept-band frequencies - the only edges _extend_edges() may extend.
        band_freqs: set[int] = set()
        for c in plain_channels:
            if c.freq_hz not in bw_map:
                _claim(c.freq_hz, mem_bw, c.modulation, c.name, c.weight)
            default_freqs.append(c.freq_hz)
        for vr in plain_bands:
            freqs, band_mod = _band_grid(vr)
            if len(freqs) > MAX_VFO_CHANNELS:
                keep_every = -(-len(freqs) // MAX_VFO_CHANNELS)
                freqs = freqs[::keep_every]
                self.signals.log.emit(
                    tr("Too many channels in band \"{label}\" ({count}) - thinned every {every}.").format(
                        label=vr.label, count=len(freqs) * keep_every, every=keep_every,
                    )
                )
            for f in freqs:
                if f not in bw_map:
                    _claim(f, float(max(1, vr.step_hz)), band_mod, weight=vr.weight)
                default_freqs.append(f)
                freq_to_band[f] = vr
                # Single-point "band": no edge to extend (gotcha 31).
                if vr.start_hz != vr.stop_hz:
                    band_freqs.add(f)

        default_grouping_bw = max((bw_map[f] for f in default_freqs), default=25_000.0)
        default_windows = plan_windows(default_freqs, sample_rate, default_grouping_bw)
        _extend_edges(default_windows, default_grouping_bw, band_freqs)
        for w in default_windows:
            # VfoRangeConfig is a plain (mutable, unhashable) dataclass, so
            # identity-compare (`is`) rather than building a set of them.
            owners = [freq_to_band[f] for f in w.freqs if f in freq_to_band]
            if owners and all(b is owners[0] for b in owners):
                w.band = owners[0]

        # Display names from all channels, enabled or not, matched by rounded
        # kHz - like the Recordings page: a disabled channel can still be
        # caught by a band sweep, and a typed-in frequency rarely hits a
        # band's grid to the Hz.
        name_by_khz: dict[int, str] = {}
        for c in self.channels:
            if c.name:
                name_by_khz[round(c.freq_hz / 1000)] = c.name

        with self._lock:
            self._priority_lanes = priority_lanes
            self._default_lane = _Lane(weight=0.0, windows=default_windows, is_range=False)
            self._scheduler = _WeightedScheduler(priority_lanes) if priority_lanes else None
            self._macro_cycle_counter = 0.0
            self._default_due = False
            self._channel_bw_map = bw_map
            self._channel_name_map = name_map
            self._channel_name_by_khz = name_by_khz
            self._channel_mod_map = mod_map
            self._channel_weight_map = weight_map

        total_windows = len(default_windows) + sum(len(lane.windows) for lane in priority_lanes)
        extra = (
            " " + tr("({count} priority-weighted entries)").format(count=len(priority_lanes))
            if priority_lanes else ""
        )
        self.signals.log.emit(
            tr("Wideband scanning: {channels} channels in {windows} capture window(s) ({mhz:.2f} MHz each){extra}.").format(
                channels=len(bw_map), windows=total_windows, mhz=sample_rate / 1e6, extra=extra,
            )
        )

    def _chunk_size(self) -> int:
        return max(1, int(self.config.device.sample_rate_hz * CHUNK_SECONDS))

    def _is_blacklisted(self, freq_hz: int) -> bool:
        return any(e.contains(freq_hz) for e in self.blacklist)

    def _maybe_apply_gain(self, window: ScanWindow | None) -> None:
        """Applies the effective gain mode, polled every iteration (gotcha
        27): "manual" and "auto" (hardware AGC) go straight to the backend;
        entering "sw_auto" starts a SoftwareAgc at the configured gain, which
        _maybe_run_software_agc() then steps every tick."""
        mode, db = self._effective_gain(window)
        if mode == "sw_auto":
            if self._applied_gain_mode != "sw_auto" or self._software_agc is None:
                self._software_agc = SoftwareAgc(db)
                self._backend.set_gain("manual", db)
                self._applied_gain_mode, self._applied_gain_db = "sw_auto", db
            return
        self._software_agc = None
        if mode != self._applied_gain_mode or db != self._applied_gain_db:
            self._backend.set_gain(mode, db)
            self._applied_gain_mode, self._applied_gain_db = mode, db

    def _maybe_run_software_agc(self, iq: np.ndarray) -> None:
        """Steps the SoftwareAgc from this tick's IQ peak; no-op unless "sw_auto"."""
        if self._software_agc is None or iq.size == 0:
            return
        peak = float(np.max(np.abs(iq)))
        old_gain_db = self._software_agc.gain_db
        new_gain = self._software_agc.step(peak, self._backend.gain_range_db)
        if new_gain is not None:
            self._backend.set_gain("manual", new_gain)
            self._applied_gain_db = new_gain
            # Pre-scale each slot's output AGC so the step isn't heard.
            factor = 10.0 ** ((new_gain - old_gain_db) / 20.0)
            for slot in self._slots.values():
                slot.demod.notify_rf_gain_change(factor)
                # A gain step moves every channel's dB level together -
                # left in the history it reads as "correlated" to
                # _detect_reflections() and got real signals discarded.
                slot.level_history.clear()

    def _maybe_follow_schedule(self) -> None:
        """Rebuilds the windows when a schedule entry starts or ends."""
        now = time.monotonic()
        if now < self._schedule_check_at:
            return
        self._schedule_check_at = now + 5.0
        sig = schedule.signature(self.config)
        if sig != self._schedule_logged and self.config.schedule.enabled:
            entries = self.config.schedule.entries
            names = [entries[i].name or f"{entries[i].start}-{entries[i].end}" for i in sig[2]]
            if names:
                text = tr("Schedule: now {names}.").format(names=", ".join(names))
            elif self.config.schedule.outside == "normal":
                text = tr("Schedule: outside the time ranges - scanning as usual.")
            else:
                text = tr("Schedule: nothing planned right now - scanning paused.")
            self.signals.log.emit(text)
        self._schedule_logged = sig
        if sig != self._schedule_sig:
            self._rebuild_windows()

    def _next_window(self) -> ScanWindow | None:
        self._maybe_follow_schedule()
        with self._lock:
            for _ in range(MAX_PICK_ATTEMPTS):
                window = self._pick_next_locked()
                if window is None:
                    return None
                if any(not self._is_blacklisted(f) for f in window.freqs):
                    return window
            return None

    def _due_recency_boost_locked(self) -> ScanWindow | None:
        """A boosted window whose extra visit is due, if any. Checked before
        the normal rotation, but only ever one extra pick in between."""
        if not self._recency_boosts:
            return None
        now = time.monotonic()
        expired = [k for k, b in self._recency_boosts.items() if now >= b["expires_at"]]
        for k in expired:
            del self._recency_boosts[k]
        if not self.config.recency_boost.enabled:
            return None
        for boost in self._recency_boosts.values():
            if now >= boost["next_due"]:
                boost["next_due"] = now + boost["interval"]
                return boost["window"]
        return None

    def _pick_next_locked(self) -> ScanWindow | None:
        """Call with self._lock held; rules in _rebuild_windows()."""
        boosted = self._due_recency_boost_locked()
        if boosted is not None:
            return boosted

        if self._default_due and self._default_lane.windows:
            self._default_due = False
            return self._default_lane.advance()

        if self._scheduler is not None and self._scheduler.lanes:
            lane = self._scheduler.pick()
            window = lane.advance()
            if lane.is_range and lane.just_wrapped:
                self._default_due = True
            elif not any(l.is_range for l in self._scheduler.lanes):
                # No range-lane to hook the "give weight-0 stuff a turn" event
                # onto - fall back to once per full weighted-scheduler cycle.
                self._macro_cycle_counter += 1
                if self._macro_cycle_counter >= self._scheduler.total_weight:
                    self._macro_cycle_counter = 0.0
                    self._default_due = True
            return window

        return self._default_lane.advance()

    def _emit_state(self, state: ScannerState) -> None:
        """Emits only actual changes - SCANNING re-derives it every tick."""
        if state == self._last_emitted_state:
            return
        self._last_emitted_state = state
        self.signals.state_changed.emit(state.value)

    def _display_state(self) -> ScannerState:
        """Status shown in the UI, derived from the slots; doesn't drive the loop."""
        if not self._slots:
            return ScannerState.SCANNING
        if any(s.linger_deadline is None for s in self._slots.values()):
            return ScannerState.SIGNAL_DETECTED
        return ScannerState.LINGER

    # A spectrum frame every tick (~33/s) once caused enough GIL contention
    # to overflow AcquisitionThread's queue (gotcha 30).
    _SPECTRUM_FPS = 10.0

    def _submit_spectrum(
        self, iq: np.ndarray, sample_rate: float, center_hz: float, trigger_dbfs: float, custom: bool = False,
    ) -> None:
        """Hands the IQ block to _SpectrumWorker, at most _SPECTRUM_FPS times
        a second - a skipped frame only affects the display."""
        now = time.monotonic()
        if now - self._last_spectrum_submit_at < 1.0 / self._SPECTRUM_FPS:
            return
        self._last_spectrum_submit_at = now
        self._spectrum_worker.submit(iq, sample_rate, center_hz, trigger_dbfs, custom)

    def _emit_freqs(self) -> None:
        """What the frequency display shows: every
        currently active channel (primary - the audible one - first, the
        rest by descending weight), or a placeholder describing the tuned
        window while nothing's active yet."""
        order = sorted(
            self._slots.keys(),
            key=lambda f: (0 if f == self._primary_freq else 1, -self._channel_weight_map.get(f, 0.0)),
        )
        entries = [(f, self._slots[f].name) for f in order]
        if not entries and self._cur_window is not None:
            window = self._cur_window
            if len(window.freqs) == 1:
                label = self._channel_name_map.get(window.freqs[0], "")
            else:
                label = tr("{count} channels in window").format(count=len(window.freqs))
            entries = [(window.center_hz, label)]
        self.signals.freq_changed.emit(entries)

    def _main_loop(self) -> None:
        _checkpoint("_main_loop() entered")
        state = ScannerState.CALCULATE_NEXT_FREQ
        self._emit_state(state)
        tuning_deadline = scanning_deadline = 0.0
        sample_rate = self.config.device.sample_rate_hz

        while not self._stop_evt.is_set():
            self._maybe_apply_gain(self._cur_window)
            chunk = self._chunk_size()

            if state == ScannerState.CALCULATE_NEXT_FREQ:
                self._checkpoint_once("first CALCULATE_NEXT_FREQ")
                window = self._next_window()
                if window is None:
                    time.sleep(0.3)
                    continue
                self._cur_window = window
                self._emit_freqs()  # no slots active here (guaranteed - see below) -> shows the window placeholder
                if window.center_hz == self._last_tuned_center_hz:
                    # Already tuned here (narrow or solo band): skip the
                    # retune, the settle wait and a bogus cycle time.
                    scanning_deadline = time.monotonic() + self.config.timing.scanning_dwell_ms / 1000.0
                    state = ScannerState.SCANNING
                    self._emit_state(state)
                else:
                    self._backend.set_frequency(window.center_hz)
                    self._last_tuned_center_hz = window.center_hz
                    self._checkpoint_once("first set_frequency() done")
                    now = time.monotonic()
                    last_visit = self._window_last_visit.get(window.center_hz)
                    self._window_last_visit[window.center_hz] = now
                    if last_visit is not None:
                        self.signals.cycle_time_changed.emit(now - last_visit)
                    tuning_deadline = time.monotonic() + self._effective_tuning_time_ms(window) / 1000.0
                    state = ScannerState.TUNING
                    self._emit_state(state)

            elif state == ScannerState.TUNING:
                self._checkpoint_once("first TUNING read() about to run")
                self._backend.read(chunk)  # discard: head is muted while the tuner settles
                self._checkpoint_once("first TUNING read() returned")
                self._skip_evt.clear()
                self._skip_lockout_evt.clear()
                if time.monotonic() >= tuning_deadline:
                    scanning_deadline = time.monotonic() + self.config.timing.scanning_dwell_ms / 1000.0
                    state = ScannerState.SCANNING
                    self._emit_state(state)

            elif state == ScannerState.SCANNING:
                self._checkpoint_once("first SCANNING read() about to run")
                read_start = time.monotonic() if _DEBUG_TIMING else 0.0
                iq = self._backend.read(chunk)
                self._report_dropped_chunks()
                self._checkpoint_once("first SCANNING read() returned")
                if _DEBUG_TIMING:
                    self._record_read_timing(time.monotonic() - read_start)
                tick_start = time.monotonic() if _DEBUG_TIMING else 0.0
                window = self._cur_window
                self._feed_raw_iq(iq, window.center_hz)
                self._maybe_run_software_agc(iq)

                # One FFT, shared by both steps below.
                spectrum = spectrum_cumsum(iq, sample_rate)

                # 1. Advance whatever's already active (demod/play/record,
                # decide continue/linger/close) - see _service_active_slots.
                primary_level = self._service_active_slots(iq, spectrum, sample_rate, window)
                any_active = bool(self._slots)
                if not any_active:
                    # Idle tick: keep a manual recording continuous with silence.
                    self._feed_manual_recording(
                        np.zeros(max(1, round(AUDIO_RATE * CHUNK_SECONDS)), dtype=np.float32)
                    )

                # 2. New activity - a first detection in this window, or one
                # more channel next to those already active.
                not_blacklisted = ~self._blacklist_mask(window.freqs)
                # A broadcast FM station fills ~±100 kHz, so the channels
                # next to an active WFM slot only see its sidebands - never
                # a second station (a constant-envelope FM sideband also
                # slips past _detect_reflections, which needs level swings).
                wfm_active = [
                    s.freq_hz for s in self._slots.values() if s.modulation == Modulation.WFM
                ]
                candidate_freqs = [
                    f for f, ok in zip(window.freqs, not_blacklisted)
                    if ok and f not in self._slots
                    and not any(abs(f - w) < _WFM_GUARD_HZ for w in wfm_active)
                ]
                raw_powers: dict[int, float] = {}
                best_freq = best_level = None
                if candidate_freqs:
                    raw_powers = channel_powers_from_spectrum(
                        spectrum, window.center_hz, candidate_freqs, self._channel_bw_map,
                    )
                    # Edge-corrected for triggering only; raw_powers go to logs/metadata.
                    corrected = self._apply_edge_compensation(raw_powers, window.center_hz)
                    best_freq, best_level = _strongest(corrected)

                effective_trigger = self._effective_trigger_dbfs(window)
                self.signals.level_changed.emit(
                    primary_level if primary_level is not None
                    else (best_level if best_level is not None else -120.0)
                )
                self._submit_spectrum(
                    iq, sample_rate, window.center_hz, effective_trigger,
                    window.band is not None and window.band.has_advanced_overrides,
                )
                self._emit_state(self._display_state())

                if self._skip_evt.is_set() or self._skip_lockout_evt.is_set():
                    lockout = self._skip_lockout_evt.is_set()
                    if any_active:
                        self._skip_all_slots(lockout)
                    elif lockout and best_freq is not None:
                        self.signals.blacklisted.emit(best_freq)
                    self._skip_evt.clear()
                    self._skip_lockout_evt.clear()
                    if self._hold:  # Skip means "leave" - a hold would just reopen it next tick
                        self.set_hold(False)
                    state = ScannerState.CALCULATE_NEXT_FREQ
                    self._emit_state(state)
                elif (
                    best_freq is not None and best_level >= effective_trigger
                    and self._at_recording_capacity()
                ):
                    now = time.monotonic()
                    if now - self._capacity_logged.get(best_freq, 0.0) > 30.0:
                        self._capacity_logged[best_freq] = now
                        self.signals.log.emit(
                            tr("Limit of {n} simultaneous recordings reached - {freq:.4f} MHz not recorded").format(
                                n=self.config.recording.max_concurrent_recordings, freq=best_freq / 1e6,
                            )
                        )
                    if not any_active and not self._hold and now >= scanning_deadline:
                        state = ScannerState.CALCULATE_NEXT_FREQ
                        self._emit_state(state)
                elif (
                    best_freq is not None and best_level >= effective_trigger
                    and not self._at_recording_capacity()
                ):
                    raw_level = raw_powers[best_freq]
                    self._calibration_log.log_detect(
                        best_freq, window.center_hz, sample_rate, self._applied_gain_mode,
                        self._applied_gain_db, raw_level, effective_trigger,
                        self._channel_bw_map.get(best_freq, 0.0),
                    )
                    self._open_slot(best_freq, window, raw_level)
                elif not any_active and not self._hold and time.monotonic() >= scanning_deadline:
                    # Nothing active and the dwell is over - move on.
                    self._maybe_log_floor(iq, sample_rate, window)
                    state = ScannerState.CALCULATE_NEXT_FREQ
                    self._emit_state(state)

                if _DEBUG_TIMING:
                    self._record_tick_timing(time.monotonic() - tick_start)

        # Stop requested mid-transmission: make sure any open segment(s) are closed.
        for freq_hz in list(self._slots.keys()):
            self._close_slot(freq_hz, reason="stopped")
        self._emit_state(ScannerState.IDLE)

    def _record_read_timing(self, elapsed: float) -> None:
        """Debug: how long backend.read() blocks; a stall gets its own line."""
        self._read_n += 1
        if elapsed > CHUNK_SECONDS:
            self._read_over_budget += 1
        self._read_max_s = max(self._read_max_s, elapsed)
        if elapsed > _READ_STALL_LOG_THRESHOLD_S:
            print(
                f"[scanner-timing STALL {time.strftime('%H:%M:%S')}] backend.read() blocked for "
                f"{elapsed * 1000:.0f}ms (budget {CHUNK_SECONDS * 1000:.0f}ms) - active_slots={len(self._slots)}",
                flush=True,
            )

    def _record_tick_timing(self, elapsed: float) -> None:
        """Debug: once a second, ticks and reads that overran CHUNK_SECONDS.
        Slow processing = too much work per tick; slow reads = the SDR/USB
        isn't delivering in real time."""
        self._tick_n += 1
        if elapsed > CHUNK_SECONDS:
            self._tick_over_budget += 1
        self._tick_max_s = max(self._tick_max_s, elapsed)
        now = time.monotonic()
        if now - self._tick_report_at < 1.0:
            return
        print(
            f"[scanner-timing {time.strftime('%H:%M:%S')}] ticks={self._tick_n} "
            f"over_budget={self._tick_over_budget} max_tick={self._tick_max_s * 1000:.1f}ms "
            f"budget={CHUNK_SECONDS * 1000:.0f}ms active_slots={len(self._slots)} | "
            f"read_over_budget={self._read_over_budget}/{self._read_n} max_read={self._read_max_s * 1000:.1f}ms",
            flush=True,
        )
        self._tick_n = self._tick_over_budget = 0
        self._tick_max_s = 0.0
        self._read_n = self._read_over_budget = 0
        self._read_max_s = 0.0
        self._tick_report_at = now

    def _correction_db(self, offset_hz: float) -> float:
        """0 dB until there is a correction curve (edge_compensation.py)."""
        if self._edge_compensation is None:
            return 0.0
        return self._edge_compensation.correction_db(offset_hz)

    def _apply_edge_compensation(self, powers: dict[int, float], center_hz: float) -> dict[int, float]:
        """Edge-corrected powers for triggering/the level meter only - logs and
        metadata stay raw, or the correction would feed back into its own data."""
        if self._edge_compensation is None or not self._edge_compensation.has_data:
            return powers
        return {f: v + self._correction_db(f - center_hz) for f, v in powers.items()}

    # -- per-band overrides ---------------------------------------------
    # A window tagged with a band uses that band's own trigger/timing/gain/
    # noise reduction where set; None falls back to the global setting.
    def _effective_trigger_dbfs(self, window: ScanWindow | None) -> float:
        band = window.band if window is not None else None
        if band is not None and band.trigger_level_dbfs is not None:
            return band.trigger_level_dbfs
        return self.config.trigger_level_dbfs

    def _effective_tuning_time_ms(self, window: ScanWindow | None) -> int:
        band = window.band if window is not None else None
        if band is not None and band.tuning_time_ms is not None:
            return band.tuning_time_ms
        return self.config.timing.tuning_time_ms

    def _effective_linger_time_ms(self, window: ScanWindow | None) -> int:
        band = window.band if window is not None else None
        if band is not None and band.linger_time_ms is not None:
            return band.linger_time_ms
        return self.config.timing.linger_time_ms

    def _effective_gain(self, window: ScanWindow | None) -> tuple[str, float]:
        band = window.band if window is not None else None
        dev = self.config.device
        mode = band.gain_mode if band is not None and band.gain_mode is not None else dev.gain_mode
        db = band.gain_db if band is not None and band.gain_db is not None else dev.gain_db
        return mode, db

    def _effective_squelch_dbfs(self, window: ScanWindow | None) -> float | None:
        """None when squelch is off, else effective trigger + offset (offset
        clamped <= 0, so it can never mute during SIGNAL_DETECTED - gotcha 6).
        Mutes live audio and writes silence; never affects LINGER/closing."""
        band = window.band if window is not None else None
        enabled = band.squelch_enabled if band is not None and band.squelch_enabled is not None else self.config.squelch_enabled
        if not enabled:
            return None
        offset = band.squelch_offset_db if band is not None and band.squelch_offset_db is not None else self.config.squelch_offset_db
        return self._effective_trigger_dbfs(window) + min(0.0, offset)

    def _effective_noise_reduction(self, window: ScanWindow | None) -> tuple[bool, float]:
        band = window.band if window is not None else None
        nr = self.config.noise_reduction
        enabled = (
            band.noise_reduction_enabled if band is not None and band.noise_reduction_enabled is not None
            else nr.enabled
        )
        strength = (
            band.noise_reduction_strength if band is not None and band.noise_reduction_strength is not None
            else nr.strength
        )
        return enabled, strength

    def _maybe_log_floor(self, iq: np.ndarray, sample_rate: float, window: ScanWindow) -> None:
        """Background-noise snapshot of the window for CalibrationLogger,
        at most every _FLOOR_LOG_INTERVAL_S."""
        now = time.monotonic()
        if now - self._last_floor_log.get(window.center_hz, 0.0) < _FLOOR_LOG_INTERVAL_S:
            return
        self._last_floor_log[window.center_hz] = now
        powers = channel_powers_dbfs(iq, sample_rate, window.center_hz, window.freqs, self._channel_bw_map)
        self._calibration_log.log_floor(
            window.center_hz, sample_rate, self._applied_gain_mode, self._applied_gain_db, powers,
        )

    # -- slot management -----------------------------------------------
    def _service_active_slots(self, iq, spectrum, sample_rate: float, window: ScanWindow) -> float | None:
        """Advances every open slot by one IQ block. Returns the primary
        slot's level (for the level meter), or None if nothing is active."""
        if not self._slots:
            return None
        active_freqs = list(self._slots.keys())
        raw_powers = channel_powers_from_spectrum(spectrum, window.center_hz, active_freqs, self._channel_bw_map)
        trigger_dbfs = self._effective_trigger_dbfs(window)
        squelch_dbfs = self._effective_squelch_dbfs(window)
        linger_time_ms = self._effective_linger_time_ms(window)
        primary_level = None

        # Non-primary slots run in parallel on _slot_pool (gotcha 12) - each
        # owns its DSP state and only reads the shared iq. The primary slot,
        # the only one heard live, runs first on this thread, so recording-
        # only slots never compete with it (gotcha 28).
        tasks = []
        for freq_hz, slot in list(self._slots.items()):
            # Edge-corrected, so a slot near the window edge doesn't linger out early.
            level = raw_powers.get(freq_hz, -120.0) + self._correction_db(slot.offset_hz)
            slot.level_history.append(level)
            # Squelch only mutes audio; LINGER below depends on the trigger alone.
            squelched = squelch_dbfs is not None and level < squelch_dbfs
            if freq_hz == self._primary_freq:
                primary_level = level
                self._process_one_slot(slot, iq, squelched)
                tasks.append((freq_hz, slot, level, None))
            else:
                future = self._slot_pool.submit(self._process_one_slot, slot, iq, squelched)
                tasks.append((freq_hz, slot, level, future))

        for freq_hz, slot, level, future in tasks:
            if future is not None:
                future.result()  # re-raises the slot's own exception
            if level >= trigger_dbfs:
                slot.linger_deadline = None
            elif slot.linger_deadline is None:
                slot.linger_deadline = time.monotonic() + linger_time_ms / 1000.0
            elif time.monotonic() >= slot.linger_deadline:
                self._close_slot(freq_hz)
        self._detect_reflections(tasks)
        return primary_level

    def _detect_reflections(self, tasks: list[tuple[int, "_Slot", float, object]]) -> None:
        """Closes and discards a slot whose level has tracked a much stronger
        active slot too closely, for too long, to be coincidence (thresholds:
        _REFLECTION_*). Runs every tick, so it is kept cheap by construction."""
        slots = [slot for _freq, slot, _level, _future in tasks]
        if len(slots) < 2 or len(slots) > _REFLECTION_MAX_SLOTS_CHECKED:
            return
        # Per-slot stats computed once, not once per pair.
        prepared: dict[int, tuple[np.ndarray, float, float]] = {}
        for slot in slots:
            if len(slot.level_history) < _REFLECTION_MIN_OVERLAP_TICKS:
                continue
            arr = np.array(slot.level_history)
            std = float(np.std(arr))
            if std < 1e-6:
                continue  # near-constant level (e.g. a steady carrier) - correlation is meaningless here
            prepared[slot.freq_hz] = (arr, float(np.mean(arr)), std)

        to_close: list[tuple[int, int]] = []
        ready = [s for s in slots if s.freq_hz in prepared]
        for i, a in enumerate(ready):
            arr_a, mean_a, std_a = prepared[a.freq_hz]
            for b in ready[i + 1:]:
                arr_b, mean_b, std_b = prepared[b.freq_hz]
                n = min(len(arr_a), len(arr_b))
                la, lb = arr_a[-n:], arr_b[-n:]
                corr = np.corrcoef(la, lb)[0, 1]
                if not np.isfinite(corr) or corr < _REFLECTION_CORR_THRESHOLD:
                    continue
                gap = mean_a - mean_b
                if abs(gap) < _REFLECTION_MIN_GAP_DB:
                    continue
                weaker, stronger = (b, a) if gap > 0 else (a, b)
                to_close.append((weaker.freq_hz, stronger.freq_hz))
        for weak_freq, strong_freq in to_close:
            if weak_freq in self._slots:
                self.signals.log.emit(
                    tr("{weak:.4f} MHz looks like a reflection of {strong:.4f} MHz - discarding.").format(
                        weak=weak_freq / 1e6, strong=strong_freq / 1e6,
                    )
                )
                self._close_slot(weak_freq, reason="reflection")

    def _process_one_slot(self, slot: _Slot, iq: np.ndarray, squelched: bool) -> None:
        """Demodulates one slot from the shared, read-only ``iq`` (may run on
        the slot pool)."""
        audio = slot.demod.demod(iq, slot.modulation, slot.offset_hz)
        self._play_and_record_slot(slot, audio, squelched=squelched)

    def _at_recording_capacity(self) -> bool:
        """True when one more slot would exceed max_concurrent_recordings
        (0 = unlimited, gotcha 24). Only while auto-recording is on -
        otherwise it would limit live listening too."""
        limit = self.config.recording.max_concurrent_recordings
        if not self.config.recording.enabled or limit <= 0:
            return False
        return len(self._slots) >= limit

    def _open_slot(self, freq_hz: int, window: ScanWindow, level_dbfs: float) -> None:
        dev = self.config.device
        demod = Demodulator(dev.sample_rate_hz, AUDIO_RATE)
        noise_reducer = NoiseReducer(AUDIO_RATE)
        recorder = Recorder(self.config.recording, AUDIO_RATE)
        name = self._channel_name_map.get(freq_hz) or self._channel_name_by_khz.get(round(freq_hz / 1000), "")
        modulation = self._channel_mod_map.get(freq_hz, Modulation.AM)
        offset_hz = float(freq_hz - window.center_hz)
        slot = _Slot(freq_hz, name, modulation, offset_hz, demod, noise_reducer, recorder)
        slot.window = window  # remembered for _register_recency_boost on close
        self._slots[freq_hz] = slot

        # Higher weight wins the live speaker; a tie keeps whichever slot
        # already had it ("first heard" - only a strictly higher weight
        # actually takes over).
        weight = self._channel_weight_map.get(freq_hz, 0.0)
        if self._primary_freq is None or weight > self._primary_weight:
            self._primary_freq = freq_hz
            self._primary_weight = weight

        timing = self.config.timing
        nr_enabled, nr_strength = self._effective_noise_reduction(window)
        # WAV metadata (wav_metadata.py): the settings and RF conditions this
        # was captured with, per the window's effective settings. Written on close.
        slot.meta = {
            "app": "FalconSDR",
            "freq_hz": freq_hz,
            "name": name,
            "modulation": modulation.value,
            "channel_weight": weight,
            "window_center_hz": window.center_hz,
            "offset_from_center_hz": offset_hz,
            "sample_rate_hz": dev.sample_rate_hz,
            "channel_bw_hz": self._channel_bw_map.get(freq_hz),
            "trigger_level_dbfs": self._effective_trigger_dbfs(window),
            "measured_dbfs_at_trigger": level_dbfs,
            "edge_correction_db": self._correction_db(offset_hz),
            "gain_mode": self._applied_gain_mode,
            "gain_db": self._applied_gain_db,
            "device_driver": dev.driver,
            "scanning_dwell_ms": timing.scanning_dwell_ms,
            "tuning_time_ms": self._effective_tuning_time_ms(window),
            "linger_time_ms": self._effective_linger_time_ms(window),
            "noise_reduction_enabled": nr_enabled,
            "noise_reduction_strength": nr_strength,
            "opened_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        if self.config.recording.enabled:
            recorder.start_session()
            recorder.begin_transmission(freq_hz)
        self._emit_recording_changed()
        self._emit_freqs()

    def _close_slot(self, freq_hz: int, reason: str = "linger") -> None:
        slot = self._slots.pop(freq_hz, None)
        if slot is None:
            return
        finished_path = None
        if self.config.recording.enabled:
            finished_path = slot.recorder.end_transmission()
            slot.recorder.stop_session()
            if finished_path is None and reason == "linger":
                # Recorder dropped it - say so, a silent loss looks like a bug.
                self.signals.log.emit(
                    tr("{freq:.4f} MHz: nothing above the squelch level - recording discarded").format(freq=freq_hz / 1e6)
                )
            if finished_path is not None and reason == "reflection":
                # A reflection is never a real transmission. Renamed, not
                # deleted, so a false positive is recoverable; the suffix
                # keeps it out of the library.
                try:
                    Path(finished_path).rename(f"{finished_path}.rejected")
                except OSError:
                    pass
                finished_path = None
            elif finished_path is not None:
                # Metadata is optional - never let it fail the recording.
                # Must precede the mp3 transcode (gotcha 14).
                try:
                    slot.meta["close_reason"] = reason
                    slot.meta["closed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
                    slot.meta["duration_sec"] = round(time.monotonic() - slot.opened_at, 3)
                    append_metadata_chunk(Path(finished_path), slot.meta)
                except Exception:
                    pass
        if finished_path is not None:
            self._finish_recording(Path(finished_path))
        if reason == "linger":
            # Only a natural end earns a boost - not a Skip, Stop or lost device.
            self._register_recency_boost(slot.window)
        if self._primary_freq == freq_hz:
            self._reassign_primary()
        self._emit_recording_changed()
        self._emit_freqs()

    def _finish_recording(self, path: Path) -> None:
        """Emits recording_finished for a closed file - after the mp3
        transcode, which runs on the slot pool, never on this thread (gotcha 14)."""
        if self.config.recording.output_format != "mp3":
            self.signals.recording_finished.emit(str(path))
            return

        def _transcode() -> None:
            self.signals.recording_finished.emit(str(transcode_to_mp3(path) or path))

        try:
            self._slot_pool.submit(_transcode)
        except RuntimeError:  # pool already shut down - engine has stopped, nothing left to stall
            _transcode()

    def _register_recency_boost(self, window: ScanWindow) -> None:
        """Starts (or refreshes) extra visits to this window after a
        transmission ends (RecencyBoostConfig). Reads the config live, so a
        settings change applies to the next transmission."""
        cfg = self.config.recency_boost
        if not cfg.enabled:
            return
        now = time.monotonic()
        self._recency_boosts[window.center_hz] = {
            "window": window,
            "expires_at": now + cfg.duration_s,
            "next_due": now + cfg.interval_s,
            "interval": cfg.interval_s,
        }

    def _reassign_primary(self) -> None:
        if not self._slots:
            self._primary_freq = None
            self._primary_weight = -1.0
            return
        freq_hz = max(self._slots, key=lambda f: self._channel_weight_map.get(f, 0.0))
        self._primary_freq = freq_hz
        self._primary_weight = self._channel_weight_map.get(freq_hz, 0.0)

    def _emit_recording_changed(self) -> None:
        # An open slot is only a recording when a file is being written -
        # with auto-recording off it is just being listened to.
        path = self._active_recording_path()
        self.signals.recording_changed.emit(bool(path), path)

    def _active_recording_path(self) -> str:
        """Path for the REC indicator: the primary slot's, else any active one."""
        if self._primary_freq is not None and self._primary_freq in self._slots:
            return self._slots[self._primary_freq].recorder.current_path
        if self._slots:
            return next(iter(self._slots.values())).recorder.current_path
        return ""

    def _skip_all_slots(self, lockout: bool) -> None:
        for freq_hz in list(self._slots.keys()):
            self._close_slot(freq_hz, reason="blacklist_skip" if lockout else "skip")
            if lockout:
                self.signals.blacklisted.emit(freq_hz)

    def _play_and_record_slot(self, slot: _Slot, audio: np.ndarray, squelched: bool = False) -> None:
        # Squelched: mute live audio and record silence - a pause inside a
        # transmission stays a pause, not a jump cut. The Recorder drops it
        # if nothing real follows (gotcha 6).
        if squelched:
            slot.was_squelched = True
            if slot.freq_hz == self._primary_freq:
                # Manual recording gets what a listener hears: silence.
                self._feed_manual_recording(np.zeros(audio.shape[0], dtype=np.float32))
            if self.config.recording.enabled:
                # real=False: silence alone doesn't make a recording worth keeping.
                silence = np.zeros(audio.shape[0], dtype=np.float32)
                slot.recorder.write_audio(silence, real=False)
            return
        if slot.was_squelched:
            # The noise reducer is stateful and was skipped during the gap -
            # reset it, or it splices the pre-gap frame onto new audio (an
            # audible click). The demodulator ran every tick and needs no reset.
            slot.noise_reducer.reset()
            slot.was_squelched = False
        mono = self._apply_noise_reduction(slot, audio)
        if slot.freq_hz == self._primary_freq:
            self.signals.live_audio_ready.emit(mono)
        if slot.freq_hz == self._primary_freq:
            # Manual recording follows the primary slot regardless of mute.
            self._feed_manual_recording(mono, freq_hz=slot.freq_hz)
        if self.config.recording.enabled:
            slot.recorder.write_audio(mono)

    def _apply_noise_reduction(self, slot: _Slot, audio):
        enabled, strength = self._effective_noise_reduction(slot.window)
        if not enabled:
            return audio
        return slot.noise_reducer.process(audio, strength=strength)


def _strongest(powers: dict[int, float]) -> tuple[int | None, float]:
    if not powers:
        return None, -120.0
    freq = max(powers, key=powers.get)
    return freq, powers[freq]
