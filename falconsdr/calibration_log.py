"""Background log of power vs. offset in the capture window - the data
edge_compensation.py builds its roll-off curve from. JSONL records:

  "floor" - a window's per-channel power bucketed by offset from its center,
  taken only when the dwell ended with nothing triggered (pure background),
  averaged as linear power. At most every _FLOOR_LOG_INTERVAL_S per window.

  "detect" - one per opened slot: what triggered, where in the window, and
  the gain/threshold in effect.

Write failures are ignored - this must never affect scanning.
"""
from __future__ import annotations

import json
import time
from typing import Any

import numpy as np

from .config import CONFIG_DIR

LOG_PATH = CONFIG_DIR / "power_calibration.jsonl"
# Keep this from growing forever across very long-running sessions - it's
# meant to be sampled/curated by hand later (e.g. loaded into pandas), not an
# unbounded audit log. Once past this, logging just quietly stops for the
# rest of the session rather than filling the disk.
_MAX_BYTES = 50 * 1024 * 1024
_BUCKET_HZ = 100_000.0


def bucket_floor(powers: dict[int, float], center_hz: float, bucket_hz: float = _BUCKET_HZ) -> dict[int, float]:
    """Groups per-channel dBFS readings into offset-from-center buckets,
    averaged in the linear power domain (see module docstring)."""
    sums: dict[int, float] = {}
    counts: dict[int, int] = {}
    for freq_hz, dbfs in powers.items():
        bucket = int(round((freq_hz - center_hz) / bucket_hz) * bucket_hz)
        power = 10.0 ** (dbfs / 10.0)
        sums[bucket] = sums.get(bucket, 0.0) + power
        counts[bucket] = counts.get(bucket, 0) + 1
    return {b: 10.0 * np.log10(max(sums[b] / counts[b], 1e-12)) for b in sums}


class CalibrationLogger:
    def __init__(self) -> None:
        self._enabled = True

    def _append(self, record: dict[str, Any]) -> None:
        if not self._enabled:
            return
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > _MAX_BYTES:
                self._enabled = False
                return
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def log_floor(
        self, window_center_hz: float, sample_rate_hz: float, gain_mode: str,
        gain_db: float | None, powers: dict[int, float],
    ) -> None:
        buckets = bucket_floor(powers, window_center_hz)
        self._append({
            "t": "floor", "ts": time.time(),
            "window_center_hz": window_center_hz, "sample_rate_hz": sample_rate_hz,
            "gain_mode": gain_mode, "gain_db": gain_db,
            "buckets": {str(int(k)): round(v, 2) for k, v in buckets.items()},
        })

    def log_detect(
        self, freq_hz: int, window_center_hz: float, sample_rate_hz: float,
        gain_mode: str, gain_db: float | None, measured_dbfs: float,
        threshold_dbfs: float, channel_bw_hz: float,
    ) -> None:
        self._append({
            "t": "detect", "ts": time.time(),
            "freq_hz": freq_hz, "offset_hz": freq_hz - window_center_hz,
            "window_center_hz": window_center_hz, "sample_rate_hz": sample_rate_hz,
            "gain_mode": gain_mode, "gain_db": gain_db,
            "measured_dbfs": round(measured_dbfs, 2), "threshold_dbfs": threshold_dbfs,
            "channel_bw_hz": channel_bw_hz,
        })
