"""Audio recording quality analysis.

Recordings come from a squelch-triggered scanner: the file spans from the
moment noise opens to when it closes. So there's usually background (noise/
carrier) audible, plus - when someone's transmitting - a voice clearly
louder than that background. We score transmission quality as exactly that
ratio of signal level to background level (SNR), additionally penalizing
clipping and suspiciously short squelch "bursts".
"""
from __future__ import annotations

import io
import math
import subprocess
import wave
from pathlib import Path

import numpy as np

from falconsdr.audio_transcode import NO_WINDOW, ffmpeg_exe, system_env
from falconsdr.playback.models import QualityMetrics

WINDOW_SEC = 0.05          # window for computing the RMS envelope
NOISE_PERCENTILE = 15      # envelope percentile treated as the background level
SIGNAL_PERCENTILE = 92     # envelope percentile treated as the signal level
SNR_FOR_100 = 30.0         # SNR (dB) corresponding to a score of 100
CLIP_THRESHOLD_FRAC = 0.975  # threshold (as a fraction of full scale) considered clipping
CLIP_PENALTY_SCALE = 300.0
SHORT_RECORDING_SEC = 1.0
SHORT_RECORDING_PENALTY = 15.0


def _mp3_to_wav_bytes(path: Path) -> bytes:
    """Decodes an mp3 to an in-memory WAV with ffmpeg (``wave`` can't read
    mp3; PyAV is only part of the optional transcription extra). Raises on
    any failure - callers treat it as an unreadable file."""
    ffmpeg = ffmpeg_exe()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found")
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "wav", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, creationflags=NO_WINDOW, env=system_env(),
    )
    return proc.stdout


def _read_samples(path: Path) -> tuple[np.ndarray, int, float]:
    # mp3 is decoded to an in-memory WAV, so one parser handles both.
    source = io.BytesIO(_mp3_to_wav_bytes(path)) if path.suffix.lower() == ".mp3" else str(path)
    with wave.open(source, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate() or 1
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    # Duration from the bytes actually read: ffmpeg writing to a pipe puts a
    # placeholder (0x7FFFFFFF) in the header's frame count (gotcha 19).
    actual_n_frames = len(raw) // max(1, sampwidth * n_channels)
    duration_sec = actual_n_frames / framerate

    if sampwidth == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        full_scale = 32768.0
    elif sampwidth == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0
        full_scale = 128.0
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float64)
        full_scale = 2147483648.0
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth} bytes")

    if n_channels > 1 and data.size:
        usable = (data.size // n_channels) * n_channels
        data = data[:usable].reshape(-1, n_channels).mean(axis=1)

    return data, full_scale, duration_sec


def compute_waveform_peaks(path: Path, n_buckets: int = 400) -> np.ndarray | None:
    """Peak amplitude (0..1) per time bucket for the Recordings page's
    waveform, normalized to the recording's own loudest bucket (gotcha 34).
    None for an unreadable or empty file - "no data", unlike real silence."""
    try:
        data, full_scale, _ = _read_samples(path)
    except Exception:
        return None
    if data.size == 0:
        return None

    n_buckets = max(1, min(n_buckets, data.size))
    edge = data.size - (data.size % n_buckets)
    if edge == 0:
        edge = data.size
        n_buckets = 1
    trimmed = np.abs(data[:edge]) / full_scale
    peaks = trimmed.reshape(n_buckets, -1).max(axis=1)
    peak_max = float(peaks.max()) if peaks.size else 0.0
    if peak_max > 1e-4:
        peaks = peaks / peak_max
    return np.clip(peaks, 0.0, 1.0).astype(np.float32)


def analyze_wav(path: Path) -> QualityMetrics:
    """Computes quality metrics for a single wav file."""
    try:
        data, full_scale, duration_sec = _read_samples(path)
    except Exception:
        return QualityMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, readable=False)

    if data.size == 0:
        return QualityMetrics(0.0, 0.0, 0.0, 0.0, 0.0, duration_sec)

    framerate_estimate = max(1.0, data.size / duration_sec) if duration_sec else 12500.0
    window = max(1, int(framerate_estimate * WINDOW_SEC))
    window = min(window, data.size)
    n_windows = max(1, data.size // window)
    trimmed = data[: n_windows * window]
    frames = trimmed.reshape(n_windows, window)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))

    # Exact-zero windows are squelch gaps (gotcha 6), not receiver noise -
    # left in, they pull the noise floor to 0 and the SNR to nonsense.
    real_mask = rms > 0.0
    if not real_mask.any():
        # All zeros: use everything rather than an empty selection.
        real_mask = np.ones_like(rms, dtype=bool)
    real_rms = rms[real_mask]
    real_data = frames[real_mask].reshape(-1)

    noise_floor = float(np.percentile(real_rms, NOISE_PERCENTILE))
    signal_level = float(np.percentile(real_rms, SIGNAL_PERCENTILE))

    eps = 1e-6
    snr_db = 20.0 * math.log10(max(signal_level, eps) / max(noise_floor, eps))

    clip_thresh = CLIP_THRESHOLD_FRAC * full_scale
    clipping_ratio = float(np.mean(np.abs(real_data) >= clip_thresh)) if real_data.size else 0.0

    snr_component = max(0.0, min(snr_db, SNR_FOR_100)) / SNR_FOR_100 * 100.0
    clip_penalty = min(clipping_ratio * CLIP_PENALTY_SCALE, 40.0)
    short_penalty = SHORT_RECORDING_PENALTY if duration_sec < SHORT_RECORDING_SEC else 0.0
    quality_score = max(0.0, min(100.0, snr_component - clip_penalty - short_penalty))

    return QualityMetrics(
        noise_floor=noise_floor,
        signal_level=signal_level,
        snr_db=snr_db,
        clipping_ratio=clipping_ratio,
        quality_score=quality_score,
        duration_sec=duration_sec,
    )
