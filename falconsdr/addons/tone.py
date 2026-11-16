"""Shared front end of the two-tone data decoders (APRS 1200/2200 Hz, ACARS 1200/2400 Hz) working on
the 12.5 kHz audio of a slot: a streaming tone discriminator and a bit clock."""
from __future__ import annotations

import numpy as np
from scipy.signal import firwin, lfilter


class ToneDiscriminator:
    """Audio -> instantaneous frequency minus ``center_hz`` (Hz), streamed block by block.

    The audio is mixed down so that the two tones sit at -/+ half their spacing, low-passed and
    differentiated in phase; a positive value is the higher tone."""

    def __init__(self, rate: float, center_hz: float, cutoff_hz: float, smooth: int = 3):
        self.rate = float(rate)
        self.center_hz = float(center_hz)
        self._taps = firwin(41, cutoff_hz, fs=self.rate).astype(np.float64)
        self._zi = np.zeros(self._taps.size - 1, dtype=np.complex128)
        self._smooth = np.ones(smooth) / smooth
        self._smooth_zi = np.zeros(smooth - 1)
        self._n = 0                       # samples seen: keeps the mixer phase continuous
        self._prev = 1.0 + 0.0j           # last filtered sample of the previous block

    def reset(self) -> None:
        self._zi[:] = 0
        self._smooth_zi[:] = 0
        self._prev = 1.0 + 0.0j

    def process(self, audio: np.ndarray) -> np.ndarray:
        if audio.size == 0:
            return np.zeros(0)
        t = (self._n + np.arange(audio.size)) / self.rate
        self._n = (self._n + audio.size) % int(self.rate)   # the mixer repeats every second
        z = audio.astype(np.float64) * np.exp(-2j * np.pi * self.center_hz * t)
        z, self._zi = lfilter(self._taps, [1.0], z, zi=self._zi)
        prev = np.concatenate(([self._prev], z[:-1]))
        self._prev = z[-1]
        freq = np.angle(z * np.conj(prev)) * self.rate / (2 * np.pi)
        freq, self._smooth_zi = lfilter(self._smooth, [1.0], freq, zi=self._smooth_zi)
        return freq


class BitClock:
    """Turns a stream of hard tone decisions into one decision per symbol: a digital PLL that
    re-aligns itself on every tone change and samples in the middle of the symbol."""

    def __init__(self, rate: float, baud: float, gain: float = 0.3):
        self._inc = baud / rate
        self._gain = gain
        self._pll = 0.0
        self._prev = 0

    def reset(self) -> None:
        self._pll, self._prev = 0.0, 0

    def process(self, hard: np.ndarray) -> list[int]:
        out: list[int] = []
        pll, prev, inc, gain = self._pll, self._prev, self._inc, self._gain
        for s in hard.tolist():
            if s != prev:   # a tone change happens on a symbol boundary: pull the phase towards it
                pll = pll * (1.0 - gain) if pll < 0.5 else pll + (1.0 - pll) * gain
                prev = s
            before = pll
            pll += inc
            if before < 0.5 <= pll:
                out.append(s)
            if pll >= 1.0:
                pll -= 1.0
        self._pll, self._prev = pll, prev
        return out
