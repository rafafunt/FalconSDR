"""DSP helpers: dBFS level detection and AM/NFM/WFM demodulation to audio.

Airband ATC voice (118-136 MHz) is AM, hence the default. WFM gets its own
wideband path (_demod_wfm): decimating to AUDIO_RATE first would filter its
+/-75 kHz deviation away.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, lfilter, resample_poly

from .models import Modulation

_EPS = 1e-12

# WFM discriminator rate - keeps broadcast FM's ~200 kHz intact (capped to
# the capture rate).
WFM_DISCRIMINATOR_RATE = 200_000
# De-emphasis: 50 us is the European standard (the Americas use 75 us).
WFM_DEEMPHASIS_TAU_S = 50e-6

# Output AGC (_apply_agc): an envelope follower carried across calls, never a
# per-chunk peak normalization - that pulled noise-only chunks up to voice
# loudness and destroyed the real SNR (gotcha 25).
_AGC_TARGET_PEAK = 0.8
# Envelope floor: a weak signal isn't amplified into loud noise.
_AGC_MIN_ENVELOPE = 0.35
# Fast attack (~10 ms) catches transients before they clip; slow release
# (~400 ms) keeps pauses between words from being pumped up.
_AGC_ATTACK_TAU_S = 0.01
_AGC_RELEASE_TAU_S = 0.4
# Gain cap, so a long weak stretch doesn't turn into loud hiss.
_AGC_MAX_GAIN = 6.0

# SSB: Re{} of the baseband recovers the audio for either sideband; USB/LSB
# only decides which side gets centered on 0 Hz and kept by a tight lowpass,
# so the opposite side (noise, a neighbour) is rejected.
SSB_BANDWIDTH_HZ = 2700.0
_SSB_HALF_BW_HZ = SSB_BANDWIDTH_HZ / 2.0

# AM/NFM audio lowpass after demodulation. Voice ends at ~3 kHz; above it
# real recordings held only noise (~1 dB SNR) and narrowband spikes the
# noise reducer can't track. SSB already has its own filter, WFM keeps its
# full bandwidth.
VOICE_LOWPASS_HZ = 3500.0


def level_dbfs(iq: np.ndarray) -> float:
    """RMS power of a complex baseband block, expressed in dBFS (0 dB = full scale)."""
    if iq.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))
    return 20.0 * np.log10(max(rms, _EPS))


def _resample_rate(x: np.ndarray, from_rate: float, to_rate: float) -> np.ndarray:
    up = int(round(to_rate))
    down = int(round(from_rate))
    g = np.gcd(up, down)
    return resample_poly(x, up // g, down // g)


class StreamResampler:
    """resample_poly over a stream, block by block, with the same output as
    one call on the whole stream: each block is processed with enough of the
    previous one in front that the filter never sees the zero padding at a
    block edge (resample_poly alone would put a glitch every block - harmless
    for voice, but a lost symbol every 30 ms for digital voice). The price is
    a delay of half the filter, well under a millisecond."""

    _HALF_LEN = 10  # resample_poly's default: filter half-length = 10 * max(up, down) taps

    def __init__(self, from_rate: float, to_rate: float):
        up, down = int(round(to_rate)), int(round(from_rate))
        g = np.gcd(up, down)
        self.up, self.down = up // g, down // g
        self._half = -(-self._HALF_LEN * max(self.up, self.down) // self.up)  # in input samples
        self._hist = np.zeros(0, dtype=np.complex64)
        self._start = 0   # stream index of _hist[0], always a multiple of down
        self._next = 0    # next output index to emit

    def process(self, x: np.ndarray) -> np.ndarray:
        seg = np.concatenate([self._hist, x])
        end = self._start + seg.size
        y = resample_poly(seg, self.up, self.down)
        first = self._start * self.up // self.down  # output index of y[0]
        last = (end - self._half) * self.up // self.down  # outputs from here on still see the edge
        lo = max(self._next, first)
        out = y[lo - first:max(lo, last) - first]
        self._next = max(self._next, last)
        keep_from = (self._next * self.down // self.up - 2 * self._half) // self.down * self.down
        keep_from = max(self._start, keep_from)
        self._hist = seg[keep_from - self._start:]
        self._start = keep_from
        return out


class Demodulator:
    """Converts IQ at `sample_rate` into mono audio at `audio_rate`, keeping
    state (DC estimate, FM phase, filter memory) across calls so a burst
    starting mid-block doesn't click.
    """

    def __init__(self, sample_rate: float, audio_rate: int = 12500):
        self.sample_rate = sample_rate
        self.audio_rate = audio_rate
        self._dc_zi = np.zeros(1)  # lfilter state for the AM DC-blocker, see demod()
        self._prev_phase = 0.0
        self._mix_index = 0
        self._deemph_zi = np.zeros(1)
        self._agc_envelope = _AGC_MIN_ENVELOPE
        # Filters built on first use.
        self._ssb_ba = None
        self._ssb_zi = None
        self._ssb_mix_index = 0
        self._voice_lpf_ba = None
        self._voice_lpf_zi = None
        self._stream_resampler: StreamResampler | None = None  # discriminate() only

    def reset(self) -> None:
        self._prev_phase = 0.0
        self._mix_index = 0
        self._dc_zi = np.zeros(1)
        self._deemph_zi = np.zeros(1)
        self._agc_envelope = _AGC_MIN_ENVELOPE
        self._ssb_zi = None
        self._ssb_mix_index = 0
        self._voice_lpf_zi = None

    def _ensure_ssb_filter(self) -> None:
        if self._ssb_ba is not None:
            return
        nyq = self.audio_rate / 2.0
        self._ssb_ba = butter(4, min(_SSB_HALF_BW_HZ / nyq, 0.99), btype="lowpass")
        self._ssb_zi = np.zeros(max(len(self._ssb_ba[0]), len(self._ssb_ba[1])) - 1)

    def _demod_ssb(self, baseband: np.ndarray, sideband: Modulation) -> np.ndarray:
        """USB/LSB product detector. demod() has already centered the wanted
        sideband on 0 Hz; after the lowpass, that shift must be undone
        before Re{}, or the audio comes out pitch-shifted."""
        self._ensure_ssb_filter()
        baseband, self._ssb_zi = lfilter(*self._ssb_ba, baseband, zi=self._ssb_zi)

        extra = _SSB_HALF_BW_HZ if sideband == Modulation.USB else -_SSB_HALF_BW_HZ
        n = baseband.size
        rate = int(round(self.audio_rate))
        t = (self._ssb_mix_index + np.arange(n)) / self.audio_rate
        baseband = baseband * np.exp(2j * np.pi * extra * t)
        self._ssb_mix_index = (self._ssb_mix_index + n) % rate

        return baseband.real

    def _apply_voice_lowpass(self, audio: np.ndarray) -> np.ndarray:
        if self._voice_lpf_ba is None:
            nyq = self.audio_rate / 2.0
            self._voice_lpf_ba = butter(4, min(VOICE_LOWPASS_HZ / nyq, 0.99), btype="lowpass")
            self._voice_lpf_zi = np.zeros(max(len(self._voice_lpf_ba[0]), len(self._voice_lpf_ba[1])) - 1)
        audio, self._voice_lpf_zi = lfilter(*self._voice_lpf_ba, audio, zi=self._voice_lpf_zi)
        return audio

    def _decimate(self, x: np.ndarray) -> np.ndarray:
        # Resample *before* demodulating: resample_poly's anti-alias filter is
        # what keeps neighbouring channels out (gotcha 7). AM/NFM only.
        return _resample_rate(x, self.sample_rate, self.audio_rate)

    def _deemphasize(self, audio: np.ndarray, rate: float, zi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        dt = 1.0 / rate
        alpha = dt / (WFM_DEEMPHASIS_TAU_S + dt)
        return lfilter([alpha], [1.0, -(1.0 - alpha)], audio, zi=zi)

    def _demod_wfm(self, iq: np.ndarray) -> np.ndarray:
        # Discriminate at a wide rate (full +/-75 kHz swing), de-emphasize,
        # then narrow the audio down to audio_rate.
        intermediate_rate = min(WFM_DISCRIMINATOR_RATE, int(round(self.sample_rate)))
        baseband = _resample_rate(iq, self.sample_rate, intermediate_rate)
        phase = np.angle(baseband)
        phase = np.unwrap(np.concatenate(([self._prev_phase], phase)))
        self._prev_phase = phase[-1]
        composite = np.clip(np.diff(phase), -3.0, 3.0)

        audio, self._deemph_zi = self._deemphasize(composite, intermediate_rate, self._deemph_zi)
        if intermediate_rate != self.audio_rate:
            audio = _resample_rate(audio, intermediate_rate, self.audio_rate)
        return audio

    def discriminate(self, iq: np.ndarray, offset_hz: float, rate: int) -> np.ndarray:
        """Raw FM discriminator output at ``rate`` (radians/sample) - what a
        digital voice decoder needs: no voice filter, no de-emphasis, no AGC."""
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        if offset_hz:
            n = iq.size
            t = (self._mix_index + np.arange(n)) / self.sample_rate
            iq = iq * np.exp(-2j * np.pi * offset_hz * t)
            self._mix_index = (self._mix_index + n) % int(round(self.sample_rate))
        if self._stream_resampler is None:
            self._stream_resampler = StreamResampler(self.sample_rate, rate)
        baseband = self._stream_resampler.process(iq)
        phase = np.unwrap(np.concatenate(([self._prev_phase], np.angle(baseband))))
        self._prev_phase = phase[-1]
        return np.diff(phase).astype(np.float32)

    def demod(self, iq: np.ndarray, modulation: Modulation, offset_hz: float = 0.0) -> np.ndarray:
        """Returns mono audio as a 1-D float32 array."""
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)

        # USB/LSB add half the SSB bandwidth to the mix, centering the wanted
        # sideband on 0 Hz - even when offset_hz is 0.
        total_shift_hz = offset_hz
        if modulation == Modulation.USB:
            total_shift_hz += _SSB_HALF_BW_HZ
        elif modulation == Modulation.LSB:
            total_shift_hz -= _SSB_HALF_BW_HZ

        if total_shift_hz:
            # The tuner sits on the window center - mix this channel to baseband.
            n = iq.size
            fs = int(round(self.sample_rate))
            t = (self._mix_index + np.arange(n)) / self.sample_rate
            iq = iq * np.exp(-2j * np.pi * total_shift_hz * t)
            self._mix_index = (self._mix_index + n) % fs

        if modulation == Modulation.WFM:
            audio = self._demod_wfm(iq)
        else:
            baseband = self._decimate(iq)
            if modulation in (Modulation.USB, Modulation.LSB):
                audio = self._demod_ssb(baseband, modulation)
            elif modulation == Modulation.AM:
                # DC-block the envelope (one-pole: dc += alpha * (v - dc)).
                env = np.abs(baseband)
                alpha = 0.05
                dc, self._dc_zi = lfilter([alpha], [1.0, -(1.0 - alpha)], env, zi=self._dc_zi)
                audio = env - dc
                audio = self._apply_voice_lowpass(audio)
            else:
                phase = np.angle(baseband)
                phase = np.unwrap(np.concatenate(([self._prev_phase], phase)))
                self._prev_phase = phase[-1]
                audio = np.clip(np.diff(phase), -3.0, 3.0)
                audio = self._apply_voice_lowpass(audio)

        audio = audio.astype(np.float32)
        audio = self._apply_agc(audio)
        return np.clip(audio, -1.0, 1.0).astype(np.float32)

    def _apply_agc(self, audio: np.ndarray) -> np.ndarray:
        """Scales ``audio`` so the tracked envelope reaches _AGC_TARGET_PEAK."""
        if audio.size == 0:
            return audio
        peak = float(np.max(np.abs(audio)))
        dt = audio.shape[0] / self.audio_rate
        tau = _AGC_ATTACK_TAU_S if peak > self._agc_envelope else _AGC_RELEASE_TAU_S
        coef = 1.0 - np.exp(-dt / tau)
        self._agc_envelope += (peak - self._agc_envelope) * coef
        self._agc_envelope = max(self._agc_envelope, _AGC_MIN_ENVELOPE)
        gain = min(_AGC_TARGET_PEAK / self._agc_envelope, _AGC_MAX_GAIN)
        return audio * gain

    def notify_rf_gain_change(self, linear_factor: float) -> None:
        """Pre-scales the envelope by a known RF gain step (SoftwareAgc)
        before audio at the new level arrives - otherwise the step was heard
        as a jump, like a different transmission starting."""
        self._agc_envelope = max(self._agc_envelope * linear_factor, _AGC_MIN_ENVELOPE)


class NoiseReducer:
    """Streaming spectral-gate noise reducer for demodulated audio.

    The receiver's broadband noise is in every frame, voice is intermittent.
    Per-bin noise power is estimated by minimum statistics (Martin 2001): the
    minimum of a short-EMA-smoothed power over a trailing ~1.5 s (a plain
    frame-to-frame min-follower chases random noise dips). Spectral
    subtraction with oversubtraction and a floor then attenuates bins near
    that estimate. 41 ms STFT frames at 75% overlap, stateful across calls.
    """

    FRAME_SIZE = 512
    HOP_SIZE = FRAME_SIZE // 4  # 75% overlap keeps bin-gain changes smooth
    OVER_SUBTRACTION = 2.0
    SPECTRAL_FLOOR = 0.06        # -24 dB floor: avoids gappy/gated "musical noise"
    SMOOTH_TAU_S = 0.08          # short-term power smoothing before min-tracking
    MIN_WINDOW_S = 1.5           # trailing window the noise floor is the minimum over

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        from scipy.signal.windows import hann
        self._window = hann(self.FRAME_SIZE, sym=False).astype(np.float64)
        self._norm = self._measure_ola_norm()
        self._smooth_coef = self._tau_to_coef(self.SMOOTH_TAU_S)
        self._min_window_frames = max(1, round(self.MIN_WINDOW_S * self.sample_rate / self.HOP_SIZE))
        self.reset()

    def _tau_to_coef(self, tau_s: float) -> float:
        return float(np.exp(-self.HOP_SIZE / (self.sample_rate * tau_s)))

    def _measure_ola_norm(self) -> float:
        """Overlap-add of the analysis window with itself settles to a
        constant (COLA); measure it numerically instead of assuming the
        textbook constant so a future window/hop change can't go silently
        wrong.
        """
        probe_len = self.FRAME_SIZE * 6
        probe = np.zeros(probe_len)
        pos = 0
        while pos + self.FRAME_SIZE <= probe_len:
            probe[pos:pos + self.FRAME_SIZE] += self._window
            pos += self.HOP_SIZE
        return float(probe[probe_len // 2])

    def reset(self) -> None:
        self._in_buf = np.zeros(0, dtype=np.float64)
        self._ola = np.zeros(self.FRAME_SIZE, dtype=np.float64)
        self._smoothed_power: np.ndarray | None = None
        n_bins = self.FRAME_SIZE // 2 + 1
        self._min_buf = np.full((self._min_window_frames, n_bins), np.inf, dtype=np.float64)
        self._min_buf_pos = 0
        self._min_buf_filled = 0

    def process(self, audio: np.ndarray, strength: float = 1.0) -> np.ndarray:
        """Apply noise reduction; `strength` in [0, 1] blends between the
        original spectrum (0) and the fully gated one (1), so it can be
        dialled in live without clicking.
        """
        if audio.size == 0:
            return audio
        strength = float(np.clip(strength, 0.0, 1.0))
        self._in_buf = np.concatenate([self._in_buf, audio.astype(np.float64)])

        out_hops = []
        while self._in_buf.size >= self.FRAME_SIZE:
            frame = self._in_buf[: self.FRAME_SIZE]
            self._in_buf = self._in_buf[self.HOP_SIZE:]

            spec = np.fft.rfft(frame * self._window)
            power = spec.real ** 2 + spec.imag ** 2

            if self._smoothed_power is None:
                self._smoothed_power = power.copy()
            else:
                sc = self._smooth_coef
                self._smoothed_power = sc * self._smoothed_power + (1 - sc) * power

            self._min_buf[self._min_buf_pos] = self._smoothed_power
            self._min_buf_pos = (self._min_buf_pos + 1) % self._min_window_frames
            self._min_buf_filled = min(self._min_buf_filled + 1, self._min_window_frames)
            noise_power = self._min_buf[: self._min_buf_filled].min(axis=0)

            with np.errstate(divide="ignore", invalid="ignore"):
                gated_gain = 1.0 - self.OVER_SUBTRACTION * np.sqrt(
                    noise_power / np.maximum(power, 1e-20)
                )
            gated_gain = np.clip(gated_gain, self.SPECTRAL_FLOOR, 1.0)
            gain = 1.0 - strength * (1.0 - gated_gain)

            out_frame = np.fft.irfft(spec * gain, n=self.FRAME_SIZE)
            self._ola += out_frame
            out_hops.append((self._ola[: self.HOP_SIZE] / self._norm).copy())
            self._ola = np.concatenate([self._ola[self.HOP_SIZE:], np.zeros(self.HOP_SIZE)])

        if not out_hops:
            return np.zeros(0, dtype=np.float32)
        return np.clip(np.concatenate(out_hops), -1.0, 1.0).astype(np.float32)
