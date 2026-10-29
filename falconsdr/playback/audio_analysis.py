"""Audio recording quality analysis.

Recordings come from a squelch-triggered scanner: the file spans from the
moment noise opens to when it closes. So there's usually background (noise/
carrier) audible, plus - when someone's transmitting - a voice clearly
louder than that background. We score transmission quality as exactly that
ratio of signal level to background level (SNR), additionally penalizing
clipping and suspiciously short squelch "bursts".
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
SNR_FOR_100 = 30.0         # SNR (dB) corresponding to a score of 100
CLIP_THRESHOLD_FRAC = 0.975  # threshold (as a fraction of full scale) considered clipping
CLIP_PENALTY_SCALE = 300.0
SHORT_RECORDING_SEC = 1.0
SHORT_RECORDING_PENALTY = 15.0


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
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, creationflags=NO_WINDOW, env=system_env(),
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
    edge = peaks.size - (peaks.size % n_buckets)
    peaks = peaks[:edge].reshape(n_buckets, -1).max(axis=1)
    peak_max = float(peaks.max())
    if peak_max > 1e-4:
        peaks = peaks / peak_max
    return np.clip(peaks, 0.0, 1.0).astype(np.float32)


def analyze_wav(path: Path) -> QualityMetrics:
    """Computes quality metrics for a single wav file."""
    rms_parts: list[np.ndarray] = []
    clip_parts: list[np.ndarray] = []   # clipped samples per window
    n_frames, window_len, framerate_seen = 0, 0, 0
    try:
        for data, full_scale, window, framerate in _stream_samples(path, WINDOW_SEC):
            first = n_frames == 0
            n_frames += data.size
            framerate_seen = framerate
            if first:
                window_len = min(window, data.size)  # a file shorter than one window is a single window
            n_windows = data.size // window_len
            if not n_windows:
                continue
            frames = data[: n_windows * window_len].reshape(n_windows, window_len)
            rms_parts.append(np.sqrt(np.mean(frames ** 2, axis=1)))
            clip_parts.append(np.sum(np.abs(frames) >= CLIP_THRESHOLD_FRAC * full_scale, axis=1))
    except Exception:
        return QualityMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, readable=False)

    # Duration from the bytes actually read: ffmpeg writing to a pipe puts a
    # placeholder (0x7FFFFFFF) in the header's frame count (gotcha 19).
    duration_sec = n_frames / framerate_seen if n_frames else 0.0
    if not n_frames:
        return QualityMetrics(0.0, 0.0, 0.0, 0.0, 0.0, duration_sec)

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
    snr_db = 20.0 * math.log10(max(signal_level, eps) / max(noise_floor, eps))

    clipping_ratio = float(clipped[real_mask].sum() / (real_rms.size * window_len))

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
