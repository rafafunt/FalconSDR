"""Persistent configuration for FalconSDR.

Saved to ~/.config/FalconSDR/config.json.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "FalconSDR"
CONFIG_PATH = CONFIG_DIR / "config.json"
CHANNELS_CSV = CONFIG_DIR / "channels.csv"
BLACKLIST_CSV = CONFIG_DIR / "blacklist.csv"
DEFAULT_RECORDINGS_DIR = str(Path.home() / "FalconSDR" / "recordings")


def _migrate_from_smartscanatc() -> None:
    """The app was called SmartScanATC until 2026-10: carry its settings
    over once (copy - the old folder stays as a backup), keeping the old
    default recordings folder so the library doesn't come up empty."""
    old = Path.home() / ".config" / "SmartScanATC"
    if CONFIG_DIR.exists() or not old.is_dir():
        return
    import shutil
    shutil.copytree(old, CONFIG_DIR)
    path = CONFIG_DIR / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("recording", {}).setdefault("output_dir", str(Path.home() / "SmartScanATC" / "recordings"))
        atomic_write_text(path, json.dumps(raw, indent=2))
    except (OSError, ValueError):
        pass


def dumps_chunked(data: dict, **kwargs) -> str:
    """json.dumps of a big dict, one entry per call. A single json.dumps() runs
    in C with the GIL held for the whole dump: ~200 ms for 46k recordings, which
    stalled the scan loop and the audio (the gaps in live listening)."""
    return "{" + ",".join(json.dumps(k, **kwargs) + ":" + json.dumps(v, **kwargs) for k, v in list(data.items())) + "}"


def atomic_write_text(path: Path, text: str) -> None:
    """Write-then-rename, so a crash/power cut mid-write never leaves a
    truncated file behind - a half-written config.json used to load as
    "corrupt" -> silent defaults, which the next save() then made permanent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per thread: two HTTP threads saving the same file at once must not
    # write into (or rename away) each other's temporary file.
    temporary = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        # newline="": csv already writes its own "\r\n" - no second translation on Windows.
        with temporary.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def quarantine(path: Path) -> None:
    """Moves an unreadable file aside (``.bad``) so the next save() can't overwrite
    the only copy - a typo in config.json must not turn into silent defaults
    (no password!) and then into a permanent reset."""
    try:
        path.replace(path.with_name(path.name + ".bad"))
    except OSError:
        pass


_migrate_from_smartscanatc()


def _default_candidate_languages() -> list[str]:
    """English (aviation's language) plus the user's own system language."""
    from .i18n import system_language_code
    own = system_language_code()
    return ["en"] + ([own] if own and own != "en" else [])


def _from_dict(cls, raw: dict):
    """Builds a config dataclass ignoring keys it doesn't know - a config.json
    written by a newer/older version (or pushed by a newer desktop client to
    a remote receiver) must never crash load() with an unexpected-keyword
    TypeError. Also covers stale keys of removed options ("mode",
    "insert_marker", "audio_format")."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


def _schedule_from_dict(raw: dict) -> "ScheduleConfig":
    sched = _from_dict(ScheduleConfig, raw)
    sched.entries = [_from_dict(ScheduleEntry, e) for e in sched.entries if isinstance(e, dict)]
    return sched


@dataclass
class DeviceConfig:
    driver: str = "auto"          # "auto", "rtlsdr", "simulated", or a SoapySDR driver key
    device_args: str = ""         # extra SoapySDR device args, e.g. "serial=00000001"
    sample_rate_hz: float = 2_400_000.0  # capture bandwidth; RTL-SDR handles ~2.4 MSPS
    # "manual" (gain_db), "auto" (hardware AGC) or "sw_auto" (agc.py) - the
    # default, since hardware AGC overdrives on strong stations (gotcha 27).
    gain_mode: str = "sw_auto"

    gain_db: float = 30.0
    # DC power on the antenna input for an LNA/active antenna.
    # Off by default; only while scanning - sdr_backend switches it off on close.
    bias_tee: bool = False
    # RTL-SDR through librtlsdr (Windows, macOS, the packaged Linux build): once the scanner has
    # stayed on one frequency for a few reads, keep USB transfers queued (rtlsdr_read_async)
    # instead of one blocking read at a time - between two blocking reads the dongle drops
    # samples (see sdr_backend.RtlSdrBackend). SoapySDR streams like this on its own.
    async_read: bool = True
    # Antenna range (antenna_range.py): scan only inside [range_start_hz, range_stop_hz].
    # 0 = no limit on that side, so by default there is none. Per dongle - an extra
    # dongle's dict carries the same two keys.
    range_start_hz: int = 0
    range_stop_hz: int = 0
    # Experimental: more dongles scanning at the same time, each
    # {"driver", "device_args", "bias_tee", "range_start_hz", "range_stop_hz"} like the
    # fields above; the scan plan is split between all of them
    # (scanner_engine._partition_windows, or antenna_range.split_by_range with a range).
    extra_devices: list = field(default_factory=list)


@dataclass
class VfoRangeConfig:
    # Stable id - schedule entries refer to bands by it.
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    start_hz: int = 118_000_000
    stop_hz: int = 136_000_000
    step_hz: int = 25_000
    label: str = ""       # display name in the band list (preset name or user-entered)
    enabled: bool = True  # unchecked bands stay in the list but are skipped while scanning
    modulation: str = "AM"  # Modulation.value used to demod this band's channels
    # Scan priority, as Channel.weight; 0 = plain round-robin.
    weight: float = 0.0
    # Per-band overrides of the global settings (None = use the global one) -
    # e.g. a noisy airband needs a different trigger than a quiet one.
    trigger_level_dbfs: float | None = None
    tuning_time_ms: int | None = None
    linger_time_ms: int | None = None
    gain_mode: str | None = None
    gain_db: float | None = None
    noise_reduction_enabled: bool | None = None
    noise_reduction_strength: float | None = None
    squelch_enabled: bool | None = None
    squelch_offset_db: float | None = None

    @property
    def has_advanced_overrides(self) -> bool:
        """True when any override is set - the spectrum then draws the
        trigger line in an accent color."""
        return any((
            self.trigger_level_dbfs is not None, self.tuning_time_ms is not None,
            self.linger_time_ms is not None, self.gain_mode is not None,
            self.gain_db is not None, self.noise_reduction_enabled is not None,
            self.noise_reduction_strength is not None, self.squelch_enabled is not None,
            self.squelch_offset_db is not None,
        ))


@dataclass
class TimingConfig:
    tuning_time_ms: int = 60
    scanning_dwell_ms: int = 45
    linger_time_ms: int = 5000


@dataclass
class RecordingConfig:
    # Off until the user opts in - nothing is written to disk before that.
    enabled: bool = False
    output_dir: str = DEFAULT_RECORDINGS_DIR
    # "wav" or "mp3" - a transcode after the WAV closes; without ffmpeg the
    # WAV is kept (gotcha 14).
    output_format: str = "wav"
    # Simultaneous auto-recordings, 0 = unlimited (gotcha 24). A capped
    # channel opens on a later tick once a slot frees up.
    max_concurrent_recordings: int = 3
    # Disk retention (see playback/index.py's Library.apply_retention) - 0 = off.
    retention_days: int = 0
    retention_max_gb: float = 0.0
    retention_keep_favorites: bool = True
    # Only ever auto-delete a recording some connected computer already
    # holds a verified copy of (see worker.py's mirroring).
    retention_require_mirrored: bool = False


@dataclass
class LocationConfig:
    """Receiver location, only for the FlightRadar24/ADSBExchange links on
    the Recordings page - approximate is enough. None = not set; no default
    on purpose."""
    home_lat: float | None = None
    home_lon: float | None = None


@dataclass
class RecencyBoostConfig:
    """After a transmission ends naturally its window is revisited more
    often for a while - replies (ATC back-and-forth) follow within seconds.
    Unlike a longer linger, the rest of the scan keeps running."""
    enabled: bool = True
    duration_s: float = 90.0   # after the transmission ends
    interval_s: float = 8.0    # one extra visit this often


@dataclass
class ScheduleEntry:
    """A time range ("HH:MM", end < start crosses midnight, equal = all day)
    on the given weekdays (0 = Monday) and what is scanned during it. A range
    crossing midnight belongs to the day it starts on."""
    start: str = "08:00"
    end: str = "16:00"
    days: list[int] = field(default_factory=lambda: list(range(7)))
    name: str = ""
    band_ids: list[str] = field(default_factory=list)
    memory_list: bool = False


@dataclass
class ScheduleConfig:
    """When enabled, the entries active right now decide what is scanned
    instead of each band's and the Memory List's own switch (schedule.py).
    Overlapping entries scan everything any of them selects. ``outside``:
    "off" = nothing is scanned between entries, "normal" = the usual switches."""
    enabled: bool = False
    outside: str = "off"
    entries: list[ScheduleEntry] = field(default_factory=list)


@dataclass
class NoiseReductionConfig:
    enabled: bool = False
    strength: float = 0.6        # 0..1, blend between dry (0) and fully gated (1) spectrum


@dataclass
class TranscriptionConfig:
    enabled: bool = True         # automatic background transcription of new recordings (see worker.py)
    generic_model: str = "large-v3"  # transcribes every language, and detects which one it is
    # Optional: a model for English only (e.g. "jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper",
    # tuned for aviation). Empty = generic_model does English too: one download, one model in memory.
    english_atc_model: str = ""
    device: str = "auto"         # "auto" | "cuda" | "cpu"
    compute_type: str = "auto"   # "auto" -> int8_float16 on GPU, int8 on CPU
    # Languages an uncertain detection is limited to, so radio noise can't
    # pick an exotic one (transcription_engine._resolve_language).
    candidate_languages: list[str] = field(default_factory=lambda: _default_candidate_languages())
    min_duration_sec: float = 0.6  # shorter recordings (e.g. just a squelch crackle) are skipped


@dataclass
class RemoteReceiverConfig:
    """Client mode: this computer doesn't scan itself, it works for a
    FalconSDR server elsewhere - transcribing locally and/or keeping a
    local copy of its recordings (see worker.py). Off = local mode."""
    enabled: bool = False
    url: str = ""  # e.g. "http://receiver.local:8765"
    token: str = ""


@dataclass
class LegalDisclaimerConfig:
    """UI languages each legal notice has been shown in (gotcha 20) -
    switching language shows them again."""
    launch_seen_languages: list[str] = field(default_factory=list)
    recording_seen_languages: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    # Bands scanned together with the Memory List: presets, custom ranges and
    # single frequencies (start_hz == stop_hz).
    bands: list[VfoRangeConfig] = field(
        default_factory=lambda: [
            VfoRangeConfig(
                start_hz=118_000_000, stop_hz=136_975_000, step_hz=8_333,
                label="Civil aviation (118-137 MHz)",
            )
        ]
    )
    timing: TimingConfig = field(default_factory=TimingConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    recency_boost: RecencyBoostConfig = field(default_factory=RecencyBoostConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    noise_reduction: NoiseReductionConfig = field(default_factory=NoiseReductionConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    remote_receiver: RemoteReceiverConfig = field(default_factory=RemoteReceiverConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    trigger_level_dbfs: float = -30.0
    # Squelch: an offset (<= 0) from trigger_level_dbfs, never an absolute
    # level (gotcha 6). Acts only during LINGER, cutting the noise burst after
    # PTT release from live audio and the recording.
    squelch_enabled: bool = False
    squelch_offset_db: float = -6.0
    default_blacklist_margin_hz: int = 5000
    memory_channel_bandwidth_hz: int = 12_500  # per-channel power window used to watch Memory List channels
    # Master switch for the whole Memory List, above each channel's own flag.
    memory_list_enabled: bool = True
    # Memory List folders, kept so empty ones don't disappear.
    channel_folders: list[str] = field(default_factory=list)
    # "auto" (the browser's/OS's language), "en" or a code with a
    # falconsdr/locale/<code>.po file.
    language: str = "auto"
    legal_disclaimer: LegalDisclaimerConfig = field(default_factory=LegalDisclaimerConfig)
    # Offers the simulated device and enables diagnostics (gotcha 8/32).
    debug_mode: bool = False
    # Optional password for the web UI and workers (Settings → Access).
    # Empty = anyone who can reach the server can use it.
    access_token: str = ""
    # Per-computer worker settings, keyed by worker name - see worker.py:
    # {"transcribe": bool, "mirror": bool, "mirror_dir": str}
    workers: dict = field(default_factory=dict)

    @staticmethod
    def load() -> "AppConfig":
        if not CONFIG_PATH.exists():
            cfg = AppConfig()
            cfg.save()
            return cfg
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config.json is not an object")
        except ValueError:
            quarantine(CONFIG_PATH)
            return AppConfig()
        except OSError:
            return AppConfig()
        if "bands" in raw:
            bands = [_from_dict(VfoRangeConfig, b) for b in raw["bands"]]
        else:
            # Migrate pre-band-list configs (single "vfo_range" + "extra_vfo_ranges").
            legacy_primary = raw.get("vfo_range")
            bands = [_from_dict(VfoRangeConfig, legacy_primary)] if legacy_primary else AppConfig().bands
            bands += [_from_dict(VfoRangeConfig, r) for r in raw.get("extra_vfo_ranges", [])]
        cfg = AppConfig(
            device=_from_dict(DeviceConfig, raw.get("device", {})),
            bands=bands,
            timing=_from_dict(TimingConfig, raw.get("timing", {})),
            recording=_from_dict(RecordingConfig, raw.get("recording", {})),
            recency_boost=_from_dict(RecencyBoostConfig, raw.get("recency_boost", {})),
            schedule=_schedule_from_dict(raw.get("schedule", {})),
            noise_reduction=_from_dict(NoiseReductionConfig, raw.get("noise_reduction", {})),
            transcription=_from_dict(TranscriptionConfig, raw.get("transcription", {})),
            remote_receiver=_from_dict(RemoteReceiverConfig, raw.get("remote_receiver", {})),
            location=_from_dict(LocationConfig, raw.get("location", {})),
            trigger_level_dbfs=raw.get("trigger_level_dbfs", -30.0),
            squelch_enabled=raw.get("squelch_enabled", False),
            squelch_offset_db=raw.get("squelch_offset_db", -6.0),
            default_blacklist_margin_hz=raw.get("default_blacklist_margin_hz", 5000),
            memory_channel_bandwidth_hz=raw.get("memory_channel_bandwidth_hz", 12_500),
            channel_folders=list(raw.get("channel_folders", [])),
            memory_list_enabled=raw.get("memory_list_enabled", True),
            language=raw.get("language", "auto"),
            legal_disclaimer=_from_dict(LegalDisclaimerConfig, raw.get("legal_disclaimer", {})),
            debug_mode=raw.get("debug_mode", False),
            access_token=raw.get("access_token", ""),
            workers=dict(raw.get("workers", {})),
        )
        cfg.recording.output_dir = cfg.recording.output_dir.strip() or DEFAULT_RECORDINGS_DIR
        if "bands" not in raw or any("id" not in b for b in raw["bands"]):
            cfg.save()  # persist the new band ids, or they'd change on every start
        return cfg

    def save(self) -> None:
        atomic_write_text(CONFIG_PATH, json.dumps(asdict(self), indent=2, ensure_ascii=False))
