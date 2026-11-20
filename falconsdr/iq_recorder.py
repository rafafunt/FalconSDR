"""Raw wideband IQ recording (gotcha 15) in SigMF (https://sigmf.org - GNU
Radio, Inspectrum, SDR++...), for users with their own decoding.

Fed only by SCANNING reads, never by TUNING. One file can span several
retunes: SigMF's "captures" list records where each frequency starts.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

import numpy as np


class IQRecorder:
    def __init__(self, output_dir: str, sample_rate_hz: float):
        self.sample_rate_hz = sample_rate_hz
        self._lock = threading.Lock()
        self._samples_written = 0
        self._captures: list[dict] = []
        self._last_center_hz: float | None = None
        now = dt.datetime.now()
        stamp = now.strftime("%Y-%m-%d_%H-%M-%S-") + f"{now.microsecond:06d}"
        # Kept in its own subfolder, separate from the recording-library
        # WAV/MP3 tree (organizer.py/library.py only expect audio there) -
        # raw IQ is technical output for the user's own tools, not something
        # meant to show up as a playable "recording" in this app itself.
        raw_dir = Path(output_dir) / "raw_iq"
        raw_dir.mkdir(parents=True, exist_ok=True)
        base = raw_dir / f"{stamp}_iq"
        self.data_path = base.with_suffix(".sigmf-data")
        self.meta_path = base.with_suffix(".sigmf-meta")
        self._fh = open(self.data_path, "wb")

    @property
    def current_path(self) -> str:
        return str(self.data_path)

    def write(self, iq: np.ndarray, center_freq_hz: float) -> None:
        if iq.size == 0:
            return
        buf = np.ascontiguousarray(iq, dtype="<c8")
        with self._lock:
            if self._fh.closed:
                return
            if center_freq_hz != self._last_center_hz:
                self._captures.append({
                    "core:sample_start": self._samples_written,
                    "core:frequency": center_freq_hz,
                    "core:datetime": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                self._last_center_hz = center_freq_hz
            self._fh.write(memoryview(buf))
            self._samples_written += buf.size

    def close(self) -> None:
        with self._lock:
            if self._fh.closed:
                return
            self._fh.close()
            captures = list(self._captures)
        meta = {
            "global": {
                "core:datatype": "cf32_le",
                "core:sample_rate": self.sample_rate_hz,
                "core:version": "1.0.0",
                "core:recorder": "FalconSDR",
                "core:description": (
                    "Raw wideband IQ capture - may span multiple capture "
                    "windows at different center frequencies, see 'captures' below."
                ),
            },
            "captures": captures,
            "annotations": [],
        }
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
