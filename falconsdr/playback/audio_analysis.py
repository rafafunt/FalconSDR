"""Audio recording quality analysis.

Recordings come from a squelch-triggered scanner: the file spans from the
moment noise opens to when it closes. So there's usually background (noise/
carrier) audible, plus - when someone's transmitting - a voice clearly
louder than that background. The score is that signal-to-background ratio
(SNR) times the probability that the signal is speech at all (data bursts and
hiss are loud and clean but are not a transmission worth listening to), minus
a penalty for clipping and for suspiciously short squelch "bursts".

Measured on 88k library recordings against Whisper's verdict (2026-11-24): this
separates speech from no-speech with AUC ~0.88 (the SNR alone: 0.71); it does
not rank how good the speech is - nothing tried did (AUC ~0.64 vs low-confidence).
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from falconsdr.audio_transcode import NO_WINDOW, ffmpeg_exe, system_env
from falconsdr.playback.config import APP_DATA_DIR
from falconsdr.playback.models import QualityMetrics

WINDOW_SEC = 0.05          # window for computing the RMS envelope
NOISE_PERCENTILE = 15      # envelope percentile treated as the background level
SIGNAL_PERCENTILE = 92     # envelope percentile treated as the signal level
SNR_FOR_100 = 40.0         # SNR (dB) corresponding to a score of 100 (30 saturated: 44 % of all files scored exactly 100)
# Bump when the scoring changes: cached results with an older version are re-measured in the background (index.py).
ANALYSIS_VERSION = 2
# Speech probability = sigmoid(B0 + B_FLAT * ln(flatness + 0.001) + B_BAND * band_db), fitted on 900 recordings labelled by
# Whisper (speech vs no speech; 5-fold CV AUC 0.884). flatness = spectral flatness of 300-3000 Hz in the louder half of the
# windows (speech is peaky, hiss and wideband data are flat); band_db = 300-3000 Hz vs >3400 Hz density, 75th percentile.
SPEECH_B0, SPEECH_B_FLAT, SPEECH_B_BAND = -5.857, -1.342, 0.0962
CLIP_THRESHOLD_FRAC = 0.975  # threshold (as a fraction of full scale) considered clipping
CLIP_PENALTY_SCALE = 300.0
SHORT_RECORDING_SEC = 1.0
SHORT_RECORDING_PENALTY = 15.0


def cleanup_temp_decodes() -> None:
    """Removes leftover decode-*.wav temp files from a prior crash."""
    try:
        for path in APP_DATA_DIR.glob("decode-*.wav"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _mp3_to_wav_file(path: Path) -> Path:
    """Decodes an mp3 with ffmpeg into a temporary WAV on disk (``wave`` can't read mp3; PyAV is
    only part of the optional transcription extra) and returns its path - the caller deletes it.
    On disk, not in memory: a recording of hours is hundreds of MB decoded, and (unlike a pipe)
    the file gets a true frame count in its header. Raises on any failure - callers treat it as
    an unreadable file."""
    ffmpeg = ffmpeg_exe()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found")
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)  # not /tmp: that can be a RAM disk
    fd, name = tempfile.mkstemp(suffix=".wav", prefix="decode-", dir=APP_DATA_DIR)
    os.close(fd)
    try:
        subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", str(path), "-f", "wav", name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            check=True, timeout=300, creationflags=NO_WINDOW, env=system_env(),
        )
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


_SAMPLE_FORMATS = {2: (np.int16, 0.0, 32768.0), 1: (np.uint8, 128.0, 128.0), 4: (np.int32, 0.0, 2147483648.0)}
PEAK_RESOLUTION = 4_000_000  # waveform: at most this many windows per file; up to 4M frames (5+ min) exact
BLOCK_FRAMES = 1 << 20     # ~1M frames (8 MB as float64) in memory at a time


def _stream_samples(path: Path, window_sec: float = 0.0, fine_windows: int = 0):
    """Yields ``(mono float64 block, full_scale, window, framerate)`` - one block in memory at a
    time, never the whole file: a recording can last for hours (a broadcast station that never
    drops below the trigger level was once 850 MB; loading it as float64 took ~8 GB and the
    kernel killed the server on every start). Every block but the last holds whole windows:
    ``window_sec`` long, or the file split into about ``fine_windows`` of them."""
    # mp3 is decoded to a temporary WAV, so one parser handles both.
    decoded = _mp3_to_wav_file(path) if path.suffix.lower() == ".mp3" else None
    source = decoded or path
    try:
        yield from _wav_blocks(source, window_sec, fine_windows)
    finally:
        if decoded is not None:
            decoded.unlink(missing_ok=True)


def _wav_blocks(path: Path, window_sec: float, fine_windows: int):
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate() or 1
        if sampwidth not in _SAMPLE_FORMATS:
            raise ValueError(f"Unsupported sample width: {sampwidth} bytes")
        dtype, offset, full_scale = _SAMPLE_FORMATS[sampwidth]
        frame_bytes = sampwidth * n_channels
        if fine_windows:  # the header's count can be a placeholder (gotcha 19): trust the size too
            window = max(1, min(wf.getnframes(), path.stat().st_size // frame_bytes) // fine_windows)
        else:
            window = max(1, int(framerate * window_sec))
        block = window * max(1, BLOCK_FRAMES // window)
        while True:
            raw = wf.readframes(block)
            usable = len(raw) // frame_bytes * frame_bytes
            if not usable:
                return
            data = np.frombuffer(raw[:usable], dtype=dtype).astype(np.float64) - offset
            if n_channels > 1:
                data = data.reshape(-1, n_channels).mean(axis=1)
            yield data, full_scale, window, framerate


def compute_waveform_peaks(path: Path, n_buckets: int = 400) -> np.ndarray | None:
    """Peak amplitude (0..1) per time bucket for the Recordings page's
    waveform, normalized to the recording's own loudest bucket (gotcha 34).
    None for an unreadable or empty file - "no data", unlike real silence."""
    fine: list[np.ndarray] = []  # a peak per window of a few frames, so a long file stays small
    try:
        for data, full_scale, window, _ in _stream_samples(path, fine_windows=PEAK_RESOLUTION):
            usable = data.size - data.size % window
            if usable:
                fine.append(np.abs(data[:usable]).reshape(-1, window).max(axis=1) / full_scale)
            if usable < data.size:
                fine.append(np.array([np.abs(data[usable:]).max() / full_scale]))
    except Exception:
        return None
    if not fine:
        return None
    peaks = np.concatenate(fine)
    n_buckets = max(1, min(n_buckets, peaks.size))
    if peaks.size % n_buckets == 0:
        peaks = peaks.reshape(n_buckets, -1).max(axis=1)
    else:
        # Spread samples evenly across n_buckets instead of dropping up to (n_buckets - 1) trailing samples.
        edges = np.linspace(0, peaks.size, n_buckets + 1, dtype=np.intp)
        peaks = np.maximum.reduceat(peaks, edges[:-1])
    peak_max = float(peaks.max())
    if peak_max > 1e-4:
        peaks = peaks / peak_max
    return np.clip(peaks, 0.0, 1.0).astype(np.float32)


def analyze_wav(path: Path) -> QualityMetrics:
    """Computes quality metrics for a single wav file.

    Combines envelope SNR (voice peaks vs background pauses) with per-window
    voice-band spectral clarity (300-3000 Hz speech energy density vs >3400 Hz
    broadband hiss). That avoids two failure modes of envelope SNR alone:
    1) a clean, continuous voice clip with no quiet pause used to look like low SNR;
    2) broadband static with a single loud squelch pop used to score high.
    """
    rms_parts: list[np.ndarray] = []
    clip_parts: list[np.ndarray] = []   # clipped samples per window
    spec_snr_parts: list[np.ndarray] = []  # voice-band vs high-band density ratio (dB) per window
    flat_parts: list[np.ndarray] = []      # spectral flatness of the voice band per window
    n_frames, window_len, framerate_seen = 0, 0, 0
    voice_mask = high_mask = None
    try:
        for data, full_scale, window, framerate in _stream_samples(path, WINDOW_SEC):
            first = n_frames == 0
            n_frames += data.size
            framerate_seen = framerate
            if first:
                window_len = min(window, data.size)  # a file shorter than one window is a single window
                if framerate >= 8000 and window_len >= 64:
                    freqs = np.fft.rfftfreq(window_len, d=1.0 / framerate)
                    vm = (freqs >= 300.0) & (freqs <= 3000.0)
                    hm = freqs >= 3400.0
                    if vm.any() and hm.any():
                        voice_mask, high_mask = vm, hm
            n_windows = data.size // window_len
            if not n_windows:
                continue
            frames = data[: n_windows * window_len].reshape(n_windows, window_len)
            rms_parts.append(np.sqrt(np.mean(frames ** 2, axis=1)))
            clip_parts.append(np.sum(np.abs(frames) >= CLIP_THRESHOLD_FRAC * full_scale, axis=1))
            if voice_mask is not None and high_mask is not None:
                spec = np.fft.rfft(frames, axis=1)
                psd = spec.real ** 2 + spec.imag ** 2
                v_dens = np.mean(psd[:, voice_mask], axis=1)
                h_dens = np.mean(psd[:, high_mask], axis=1)
                spec_snr_parts.append(10.0 * np.log10(np.maximum(v_dens, 1e-12) / np.maximum(h_dens, 1e-12)))
                voice_psd = np.maximum(psd[:, voice_mask], 1e-12)
                flat_parts.append(np.exp(np.mean(np.log(voice_psd), axis=1)) / np.mean(voice_psd, axis=1))
    except Exception:
        return QualityMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, readable=False, version=ANALYSIS_VERSION)

    # Duration from the bytes actually read: ffmpeg writing to a pipe puts a
    # placeholder (0x7FFFFFFF) in the header's frame count (gotcha 19).
    duration_sec = n_frames / framerate_seen if n_frames else 0.0
    if not n_frames:
        return QualityMetrics(0.0, 0.0, 0.0, 0.0, 0.0, duration_sec, version=ANALYSIS_VERSION)

    rms = np.concatenate(rms_parts)
    clipped = np.concatenate(clip_parts)

    # Exact-zero windows are squelch gaps (gotcha 6), not receiver noise -
    # left in, they pull the noise floor to 0 and the SNR to nonsense.
    real_mask = rms > 0.0
    if not real_mask.any():
        # All zeros: use everything rather than an empty selection.
        real_mask = np.ones_like(rms, dtype=bool)
    real_rms = rms[real_mask]

    noise_floor = float(np.percentile(real_rms, NOISE_PERCENTILE))
    signal_level = float(np.percentile(real_rms, SIGNAL_PERCENTILE))

    eps = 1e-6
    envelope_snr_db = 20.0 * math.log10(max(signal_level, eps) / max(noise_floor, eps))

    speech_prob = 1.0
    snr_db = envelope_snr_db
    if spec_snr_parts:
        spec_snr = np.concatenate(spec_snr_parts)[real_mask]
        flatness = np.concatenate(flat_parts)[real_mask]
        # Where speech lives: the louder half of the non-silent windows (not the trailing hiss).
        active = real_rms >= float(np.percentile(real_rms, 50))
        if active.any():
            band_db = float(np.percentile(spec_snr[active], 75))
            flat_active = float(np.median(flatness[active]))
        else:
            band_db, flat_active = float(np.median(spec_snr)), float(np.median(flatness))
        z = SPEECH_B0 + SPEECH_B_FLAT * math.log(flat_active + 1e-3) + SPEECH_B_BAND * band_db
        speech_prob = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
        # A clean continuous voice has no quiet pause, so its envelope SNR is ~0 dB: the voice-band clarity stands in for it.
        snr_db = max(envelope_snr_db, min(band_db, SNR_FOR_100))

    clipping_ratio = float(clipped[real_mask].sum() / (real_rms.size * window_len))

    snr_component = max(0.0, min(snr_db, SNR_FOR_100)) / SNR_FOR_100 * 100.0
    clip_penalty = min(clipping_ratio * CLIP_PENALTY_SCALE, 40.0)
    short_penalty = SHORT_RECORDING_PENALTY if duration_sec < SHORT_RECORDING_SEC else 0.0
    quality_score = max(0.0, min(100.0, snr_component * speech_prob - clip_penalty - short_penalty))

    return QualityMetrics(
        noise_floor=noise_floor,
        signal_level=signal_level,
        snr_db=snr_db,
        clipping_ratio=clipping_ratio,
        quality_score=quality_score,
        duration_sec=duration_sec,
        speech_prob=speech_prob,
        version=ANALYSIS_VERSION,
    )
