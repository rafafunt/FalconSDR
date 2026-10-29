"""Wideband channelization: plan capture windows that cover many channels at
once (RTL-SDR can capture ~2.4 MHz in one shot), then measure per-channel
power from a single FFT per block instead of retuning channel-by-channel.

The whole 118-137 MHz airband fits in a handful of windows instead of
hundreds of retunes.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .config import VfoRangeConfig

# Fraction of the raw sample rate considered clean enough to use: RTL-SDR
# tuners roll off near the band edges and can show a DC/LO-leakage spur at
# the exact center, so we keep a margin at both edges.
USABLE_FRACTION = 0.9


@dataclass
class ScanWindow:
    center_hz: int
    freqs: list[int]
    # The band this window was built from alone, for its overrides; None when
    # mixed. Set by ScannerEngine._rebuild_windows().
    band: "VfoRangeConfig | None" = None


def plan_windows(freqs: list[int], sample_rate_hz: float, channel_bw_hz: float) -> list[ScanWindow]:
    """Greedily group a sorted set of channel frequencies into the fewest
    capture windows such that every channel in a window fits within the
    usable capture bandwidth around that window's center frequency.
    """
    if not freqs:
        return []
    usable_bw = sample_rate_hz * USABLE_FRACTION
    ordered = sorted(set(freqs))
    windows: list[ScanWindow] = []
    group: list[int] = [ordered[0]]

    def _flush(members: list[int]) -> None:
        lo, hi = members[0], members[-1]
        center = (lo + hi) // 2
        # Nudge off the exact grid mid-point so a channel doesn't land
        # precisely on the window center (DC/LO-leakage spur territory).
        center += int(channel_bw_hz * 0.37)
        windows.append(ScanWindow(center_hz=center, freqs=list(members)))

    for f in ordered[1:]:
        span = f - group[0]
        if span + channel_bw_hz <= usable_bw:
            group.append(f)
        else:
            _flush(group)
            group = [f]
    _flush(group)
    return windows


@lru_cache(maxsize=8)
def _hann_window(n: int) -> np.ndarray:
    """Cached - n is the chunk size, constant for a session. Callers must
    not modify the returned array."""
    return np.hanning(n)


@lru_cache(maxsize=8)
def _hann_window_power(n: int) -> float:
    return float(np.sum(_hann_window(n) ** 2))


@lru_cache(maxsize=8)
def _bin_freqs(n: int, sample_rate: float) -> np.ndarray:
    """FFT bin frequencies relative to the tuned center (cached, read-only)."""
    return np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / sample_rate))


def spectrum_cumsum(iq: np.ndarray, sample_rate: float) -> tuple[np.ndarray, np.ndarray] | None:
    """The FFT half of channel_powers_dbfs(), so one tick can measure
    several frequency lists from a single FFT. Returns (bin_freqs, cumsum)
    with a leading 0.0 in cumsum - a bin range's power is
    cumsum[hi] - cumsum[lo] - or None for an empty capture.
    """
    if iq.size == 0:
        return None
    n = iq.size
    window = _hann_window(n)
    win_power = _hann_window_power(n)
    spectrum = np.fft.fftshift(np.fft.fft(iq * window))
    psd = (np.abs(spectrum) ** 2) / (win_power * n)
    bin_freqs = _bin_freqs(n, sample_rate)
    cumsum = np.concatenate(([0.0], np.cumsum(psd)))
    return bin_freqs, cumsum


def channel_powers_from_spectrum(
    spectrum: tuple[np.ndarray, np.ndarray] | None, center_hz: float, freqs: list[int],
    channel_bw_hz: float | dict[int, float],
) -> dict[int, float]:
    """Per-channel power (dBFS) from a spectrum_cumsum() result.
    ``channel_bw_hz`` is one bandwidth for all, or a per-frequency dict.

    Vectorized searchsorted + cumsum: a per-bin mask took over 100 ms for a
    ~2300-channel airband window - over 3x the 30 ms tick budget.
    """
    if spectrum is None or not freqs:
        return {f: -120.0 for f in freqs}
    bin_freqs, cumsum = spectrum

    freqs_arr = np.asarray(freqs, dtype=np.float64)
    offsets = freqs_arr - center_hz
    if isinstance(channel_bw_hz, dict):
        bw = [channel_bw_hz.get(f) for f in freqs]
        if None in bw:  # a rebuild dropped a frequency a stale window / open slot still holds
            fallback = min(channel_bw_hz.values(), default=12_500.0)
            bw = [fallback if b is None else b for b in bw]
        half_bw = np.array(bw, dtype=np.float64) / 2.0
    else:
        half_bw = np.full(freqs_arr.shape, channel_bw_hz / 2.0)

    lo = np.searchsorted(bin_freqs, offsets - half_bw, side="left")
    hi = np.searchsorted(bin_freqs, offsets + half_bw, side="right")

    power = np.maximum(cumsum[hi] - cumsum[lo], 1e-12)
    db = 10.0 * np.log10(power)
    return dict(zip(freqs, db.tolist()))


def channel_powers_dbfs(
    iq: np.ndarray, sample_rate: float, center_hz: float, freqs: list[int],
    channel_bw_hz: float | dict[int, float],
) -> dict[int, float]:
    """Per-channel power from one FFT of the block. For several frequency
    lists from the same block, call spectrum_cumsum() once instead.
    """
    if iq.size == 0 or not freqs:
        return {f: -120.0 for f in freqs}
    spectrum = spectrum_cumsum(iq, sample_rate)
    return channel_powers_from_spectrum(spectrum, center_hz, freqs, channel_bw_hz)


# extend_boundary_window() probes at most this fraction of the sample rate.
_MAX_EXTEND_FRACTION = 0.15


def extend_boundary_window(
    window: ScanWindow, sample_rate_hz: float, step_hz: float, direction: str,
) -> list[int]:
    """Probe frequencies just past a band's edge (``direction`` "below" or
    "above"), within the window's unused capture room - center_hz stays, so
    no extra retune. Only for real swept bands (gotcha 31).
    """
    if not window.freqs or step_hz <= 0:
        return []
    usable_bw = sample_rate_hz * USABLE_FRACTION
    lo, hi = min(window.freqs), max(window.freqs)
    slack = usable_bw - (hi - lo + step_hz)
    extend_by = max(0.0, min(slack, sample_rate_hz * _MAX_EXTEND_FRACTION))
    n_extra = int(extend_by // step_hz)
    if n_extra <= 0:
        return []
    edge = lo if direction == "below" else hi
    sign = -1 if direction == "below" else 1
    return [int(edge + sign * step_hz * i) for i in range(1, n_extra + 1)]


def spectrum_snapshot(iq: np.ndarray, sample_rate: float, center_hz: float, num_points: int = 512) -> tuple[np.ndarray, np.ndarray]:
    """Downsampled dBFS spectrum for the UI. Returns (freqs_hz, magnitude_db).

    Each point *sums* its bins' power, like the channel measurement, so the
    trace is comparable to the trigger line - averaging read 20+ dB low.
    """
    if iq.size == 0:
        return np.zeros(num_points), np.full(num_points, -120.0)
    n = iq.size
    window = _hann_window(n)
    win_power = _hann_window_power(n)
    spectrum = np.fft.fftshift(np.fft.fft(iq * window))
    psd = (np.abs(spectrum) ** 2) / (win_power * n)
    freqs = _bin_freqs(n, sample_rate) + center_hz

    if n > num_points:
        edge = n - (n % num_points)
        psd = psd[:edge].reshape(num_points, -1).sum(axis=1)
        freqs = freqs[:edge].reshape(num_points, -1).mean(axis=1)
    db = 10.0 * np.log10(np.clip(psd, 1e-12, None))
    return freqs, db
