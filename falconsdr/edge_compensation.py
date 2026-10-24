"""Additive dB correction for the tuner's roll-off near a capture window's
edge, built from CalibrationLogger's "floor" snapshots - so one
trigger_level_dbfs gives the edge about the same sensitivity as the center.
No data yet means 0 dB everywhere.

Keyed by offset from the window center (the cause is the tuner's IF
response, not any one frequency) and by gain mode: hardware and software AGC
differ by up to ~10 dB in absolute level, and a mixed curve over/under-
corrected.

Loading is incremental (_CACHE_PATH keeps the running sums and how many
log bytes they cover): re-parsing the whole 50 MB log took 1.8 s per Start.
"""
from __future__ import annotations

import json

import numpy as np

from .calibration_log import LOG_PATH
from .config import CONFIG_DIR

_BUCKET_HZ = 100_000.0
# Fewer samples than this: too noisy, no correction for that offset.
_MIN_SAMPLES_PER_BUCKET = 20
# Real roll-off is a few dB; the cap keeps a dead bucket from being "boosted".
_MAX_CORRECTION_DB = 10.0

_CACHE_PATH = CONFIG_DIR / "edge_compensation_cache.json"


def _bucket_key(gain_mode: str, offset: int) -> str:
    return f"{gain_mode}|{offset}"


def _read_cache() -> tuple[dict[str, float], dict[str, int], int]:
    """(sums, counts, processed_bytes) keyed by _bucket_key(); empty when
    missing, unreadable or in the old format - then the log is re-read."""
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        sums = {str(k): v for k, v in raw["sums"].items()}
        counts = {str(k): v for k, v in raw["counts"].items()}
        if sums and not all("|" in k for k in sums):
            # Old format without gain mode - nothing to salvage, rebuild.
            return {}, {}, 0
        return sums, counts, int(raw["processed_bytes"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return {}, {}, 0


def _write_cache(sums: dict[str, float], counts: dict[str, int], processed_bytes: int) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps({
            "sums": sums,
            "counts": counts,
            "processed_bytes": processed_bytes,
        }), encoding="utf-8")
    except OSError:
        pass  # diagnostic cache only - never worth failing scanner startup over


def _load_floor_curve() -> dict[str, dict[int, float]]:
    """gain_mode -> {offset from center (100 kHz buckets) -> mean floor
    dBFS}, averaged as linear power. Parses only newly appended log bytes."""
    sums, counts, processed_bytes = _read_cache()

    try:
        log_size = LOG_PATH.stat().st_size
    except OSError:
        log_size = 0  # no log yet (fresh install) - nothing to add to whatever the cache already has

    if log_size < processed_bytes:
        # The log shrank (cleared or replaced) - start over.
        sums, counts, processed_bytes = {}, {}, 0

    if log_size > processed_bytes:
        try:
            with LOG_PATH.open("rb") as f:
                f.seek(processed_bytes)
                new_data = f.read()
        except OSError:
            new_data = b""
        if new_data:
            # Stop at the last newline - the trailing line may be mid-write.
            last_nl = new_data.rfind(b"\n")
            if last_nl != -1:
                # Decode first: json.loads() on bytes guesses the encoding
                # and can misread UTF-8 as UTF-16/32.
                for line in new_data[:last_nl + 1].decode("utf-8", errors="replace").splitlines():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("t") != "floor":
                        continue
                    gain_mode = rec.get("gain_mode") or "unknown"
                    for off_str, dbfs in rec.get("buckets", {}).items():
                        key = _bucket_key(gain_mode, int(off_str))
                        power = 10.0 ** (dbfs / 10.0)
                        sums[key] = sums.get(key, 0.0) + power
                        counts[key] = counts.get(key, 0) + 1
                processed_bytes += last_nl + 1
        _write_cache(sums, counts, processed_bytes)

    by_mode: dict[str, dict[int, float]] = {}
    for key in sums:
        if counts[key] < _MIN_SAMPLES_PER_BUCKET:
            continue
        gain_mode, off_str = key.split("|", 1)
        by_mode.setdefault(gain_mode, {})[int(off_str)] = 10.0 * np.log10(max(sums[key] / counts[key], 1e-12))
    return by_mode


class EdgeCompensation:
    def __init__(self, gain_mode: str = "") -> None:
        """Uses only the calibration history of this ``gain_mode``; none
        yet means 0 dB everywhere."""
        curve = _load_floor_curve().get(gain_mode or "unknown", {})
        if curve:
            reference = max(curve.values())  # the window's own strongest bucket, not assumed to be exactly offset 0
            self._correction = {
                off: min(_MAX_CORRECTION_DB, max(0.0, reference - db))
                for off, db in curve.items()
            }
        else:
            self._correction = {}

    @property
    def has_data(self) -> bool:
        return bool(self._correction)

    @property
    def bucket_count(self) -> int:
        return len(self._correction)

    def correction_db(self, offset_hz: float) -> float:
        if not self._correction:
            return 0.0
        bucket = int(round(offset_hz / _BUCKET_HZ) * _BUCKET_HZ)
        return self._correction.get(bucket, 0.0)
