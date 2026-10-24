"""Always-on, low-overhead diagnostics of SDR reads, written to a JSONL file.

Tracks reads that needed more than one readStream() call and driver
overflows - a late read lets the driver overflow, and the spliced samples
look like a broadband signal to the FFT (false detections under CPU load).

Records, one JSON object per line:

  "overflow" - one per negative readStream() return code, with how many
  samples the read already had (a splice is only possible once got > 0).

  "summary" - every _SUMMARY_INTERVAL_S: multi-call reads, overflows,
  median/max read time and the 1-minute load average.

Write failures are ignored - this must never affect scanning.
"""
from __future__ import annotations

import json
import os
import time

from .config import CONFIG_DIR

LOG_PATH = CONFIG_DIR / "sdr_io_diag.jsonl"
# Diagnostic-only, meant to be read back and discarded, not kept forever -
# same "quietly stop rather than fill the disk" policy as calibration_log.py,
# just a smaller cap since this is meant for a short, targeted capture.
_MAX_BYTES = 20 * 1024 * 1024
_SUMMARY_INTERVAL_S = 5.0


class SdrIoDiagLogger:
    def __init__(self) -> None:
        self._enabled = True
        self._window_start = time.monotonic()
        self._read_times: list[float] = []
        self._multi_call_reads = 0
        self._overflow_count = 0

    def _append(self, record: dict) -> None:
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

    @staticmethod
    def _loadavg1() -> float | None:
        # getloadavg() doesn't exist at all on Windows (AttributeError) -
        # this runs on the SDR acquisition thread, so it must never raise.
        try:
            return os.getloadavg()[0]
        except (OSError, AttributeError):
            return None

    def record_error(self, code: int, call_index: int, got_so_far: int, num_samples: int) -> None:
        """One negative-return readStream() call within an in-progress
        read(). Logged immediately (not batched into the summary) since
        these should be rare - each one is worth seeing on its own."""
        self._overflow_count += 1
        self._append({
            "t": "overflow", "ts": time.time(), "code": code,
            "call_index": call_index, "got_so_far": got_so_far,
            "num_samples": num_samples, "loadavg1": self._loadavg1(),
        })

    def record_read(self, elapsed_s: float, calls: int) -> None:
        """Called once per completed read() (not per readStream() call)."""
        self._read_times.append(elapsed_s)
        if calls > 1:
            self._multi_call_reads += 1
        now = time.monotonic()
        if now - self._window_start < _SUMMARY_INTERVAL_S:
            return
        times = sorted(self._read_times)
        n = len(times)
        self._append({
            "t": "summary", "ts": time.time(),
            "reads": n,
            "multi_call_reads": self._multi_call_reads,
            "overflow_count": self._overflow_count,
            "read_median_ms": round(times[n // 2] * 1000, 1) if n else None,
            "read_max_ms": round(times[-1] * 1000, 1) if n else None,
            "loadavg1": self._loadavg1(),
        })
        self._window_start = now
        self._read_times.clear()
        self._multi_call_reads = 0
        self._overflow_count = 0
