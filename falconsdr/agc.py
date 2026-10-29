"""Software RF gain control for the "sw_auto" gain mode (gotcha 27).

The tuner's hardware AGC reacts slowly to average level and let the ADC
overload (measured +0.7 dBFS next to a strong FM station). "sw_auto" never
uses it: ScannerEngine sets explicit manual gains computed here from the
captured IQ's peak, which works the same on any SoapySDR device.
"""
from __future__ import annotations

import math
import time


class SoftwareAgc:
    """Peak-tracking gain controller, created when "sw_auto" is entered and
    fed one peak per SCANNING tick via step().
    """

    # -6 dBFS: 6 dB of clipping margin. -9 dBFS sounded too quiet - the
    # output AGC can't make up that much.
    TARGET_PEAK = 0.501
    TARGET_PEAK_DB = 20.0 * math.log10(TARGET_PEAK)
    # Dead zone around the target, so ordinary fading doesn't move the gain.
    DEADZONE_DB = 3.0
    # A gain step is a transient of its own - at most one per second.
    MIN_ADJUST_INTERVAL_S = 1.0
    # Largest single step - a big level jump is corrected over several steps.
    MAX_STEP_DB = 6.0
    # Below this there is no real signal to measure.
    _MIN_MEASURABLE_PEAK = 1e-6
    # The peak of one 30 ms chunk of a wideband capture swings by more than the
    # dead zone on its own, so a steady station made the gain step - and the
    # dongle stall for ~100 ms, a gap in the audio - about once a second.
    # Decide on a level smoothed over this long instead.
    SMOOTH_TAU_S = 1.0
    # A peak this close to full scale is acted on at once (clipping), unsmoothed.
    OVERLOAD_PEAK = 0.97

    def __init__(self, initial_gain_db: float):
        self._gain_db = initial_gain_db
        self._last_adjust = 0.0
        self._level_db: float | None = None  # smoothed peak, dBFS
        self._level_at = 0.0

    @property
    def gain_db(self) -> float:
        return self._gain_db

    def step(self, peak_linear: float, gain_range_db: tuple[float, float]) -> float | None:
        """Takes this tick's peak |IQ| (0..1). Returns the new gain in dB,
        clamped to ``gain_range_db``, or None when nothing should change.
        """
        now = time.monotonic()
        if peak_linear <= self._MIN_MEASURABLE_PEAK:
            return None
        peak_db = 20.0 * math.log10(peak_linear)
        if self._level_db is None:
            self._level_db = peak_db
            if peak_linear < self.OVERLOAD_PEAK:
                self._last_adjust = now  # one sample is no average: let it settle first
        else:
            self._level_db += (1.0 - math.exp(-(now - self._level_at) / self.SMOOTH_TAU_S)) * (peak_db - self._level_db)
        self._level_at = now
        if now - self._last_adjust < self.MIN_ADJUST_INTERVAL_S:
            return None
        error_db = (peak_db if peak_linear >= self.OVERLOAD_PEAK else self._level_db) - self.TARGET_PEAK_DB
        if abs(error_db) <= self.DEADZONE_DB:
            return None
        step_db = max(-self.MAX_STEP_DB, min(self.MAX_STEP_DB, -error_db))
        lo, hi = gain_range_db
        new_gain = max(lo, min(hi, self._gain_db + step_db))
        if abs(new_gain - self._gain_db) < 0.05:
            return None
        self._level_db += new_gain - self._gain_db  # the level moves with the gain it was measured at
        self._gain_db = new_gain
        self._last_adjust = now
        return new_gain
