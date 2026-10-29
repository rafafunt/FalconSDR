"""Pilot-ATC recording transcription engine: language detection + Whisper
(faster-whisper), with a separate model fine-tuned for English aviation
phraseology for English recordings, and a general multilingual model for
the rest (mostly Polish).

This isn't "live" transcription - quality matters, not speed, so we
deliberately use the largest practical models and full beam search instead
of greedy/streaming decoding.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from falconsdr.config import TranscriptionConfig
from falconsdr.playback.models import Recording, TranscriptionResult

logger = logging.getLogger(__name__)

# -- loading CUDA libraries -------------------------------------------------
# ctranslate2 4.x doesn't bundle cuBLAS/cuDNN; they come from the nvidia-*-cu12
# wheels. Setting LD_LIBRARY_PATH at runtime is unreliable, so the .so files
# are preloaded with RTLD_GLOBAL before ctranslate2 dlopen()s them.
_cuda_preloaded = False


def _preload_cuda_libs() -> None:
    global _cuda_preloaded
    if _cuda_preloaded:
        return
    _cuda_preloaded = True
    try:
        import nvidia  # from the nvidia-cublas-cu12 / nvidia-cudnn-cu12 packages, if installed in the venv
    except ImportError:
        return
    base = Path(next(iter(nvidia.__path__)))
    if sys.platform == "win32":
        # The Windows wheels keep their DLLs in <pkg>/bin; ctranslate2 finds
        # them once those folders are on the DLL search path.
        for bindir in base.glob("*/bin"):
            os.add_dll_directory(str(bindir))
            os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
        return
    for sub in ("cublas/lib", "cudnn/lib", "cuda_nvrtc/lib"):
        libdir = base / sub
        if not libdir.exists():
            continue
        for so in sorted(libdir.glob("lib*.so*")):
            try:
                ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
            except OSError as exc:
                logger.debug("Failed to load %s: %s", so, exc)


# No initial_prompt with aviation phraseology on purpose: on noisy audio
# Whisper copied the prompt into the "transcription".

# Picked on real recordings. They separate good text from hallucination
# poorly, so "low_confidence" is a warning, not a reason to hide the text.
_LOW_LANG_PROBABILITY = 0.4
_LOW_AVG_LOGPROB = -0.9
_HIGH_NO_SPEECH_PROB = 0.5
_HIGH_COMPRESSION_RATIO = 2.4

_lock = threading.Lock()
_generic_model = None
_english_atc_model = None
_active_config: Optional[TranscriptionConfig] = None
# A GPU model can load fine and fail on the first inference (lazy allocation).
# Fallback order: int8_float16 on the GPU, then CPU - temporarily: VRAM
# shortages were brief spikes, not a lasting lack of memory.
_force_cpu = False
_force_cpu_since: Optional[float] = None
_gpu_compute_type: Optional[str] = None
# After this long on CPU, try the GPU again (a failure restarts the wait).
# 30 min kept a busy CPU for half an hour after a VRAM spike that was gone
# in seconds.
_GPU_RETRY_COOLDOWN_S = 120.0
# Cores left to the scanner: a saturated CPU delayed reads, spliced IQ
# showed up as false detections - which then queued more transcriptions.
_CPU_RESERVED_CORES = 4


# "auto" on the GPU: both models in float16 (~4.5 GB) ran an 8 GB card out
# of memory sooner or later; int8_float16 leaves real margin.
_DEFAULT_AUTO_GPU_COMPUTE_TYPE = "int8_float16"


def _gpu_compute_type_for(config: TranscriptionConfig) -> str:
    """The GPU compute type in use this session (int8 weights, float16 math
    for "auto")."""
    if _gpu_compute_type is not None:
        return _gpu_compute_type
    return _DEFAULT_AUTO_GPU_COMPUTE_TYPE if config.compute_type == "auto" else config.compute_type


def _is_cuda_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(token in message for token in ("cuda", "cudnn", "cublas"))


def _set_force_cpu(value: bool) -> None:
    """Sets _force_cpu together with _force_cpu_since (for counting down the
    cooldown before the next GPU attempt - see _maybe_retry_gpu_locked)."""
    global _force_cpu, _force_cpu_since
    _force_cpu = value
    _force_cpu_since = time.monotonic() if value else None


def _cpu_thread_count() -> int:
    total = os.cpu_count() or 4
    return max(1, total - _CPU_RESERVED_CORES)


def _load_model(model_id: str, config: TranscriptionConfig):
    global _gpu_compute_type
    from faster_whisper import WhisperModel

    device = "cpu" if _force_cpu else config.device
    if device in ("auto", "cuda"):
        _preload_cuda_libs()
        try:
            model = WhisperModel(
                model_id, device="cuda",
                compute_type=_gpu_compute_type_for(config),
            )
            logger.info(
                "Transcription model %s loaded on GPU (%s)",
                model_id, _gpu_compute_type_for(config),
            )
            return model
        except Exception as exc:  # noqa: BLE001 - any reason (no GPU, no VRAM, no libcublas...) -> CPU
            # The English model is loaded after the general one; with little
            # VRAM available, loading it can be the first place a memory
            # shortage shows up. Before falling back to CPU, try lighter
            # weights on the same card.
            if config.compute_type == "auto" and _gpu_compute_type_for(config) == "float16":
                _gpu_compute_type = "int8_float16"
                logger.warning(
                    "Failed to load %s on GPU in float16 (%s); "
                    "retrying on GPU in int8_float16.", model_id, exc,
                )
                return _load_model(model_id, config)
            if device == "cuda":
                raise
            logger.warning(
                "Failed to load %s on GPU even in %s (%s), "
                "falling back to CPU (will try GPU again in ~%.0f min).",
                model_id, _gpu_compute_type_for(config), exc, _GPU_RETRY_COOLDOWN_S / 60,
            )
            _set_force_cpu(True)
    ct = config.compute_type if config.compute_type != "auto" else "int8"
    cpu_threads = _cpu_thread_count()
    model = WhisperModel(model_id, device="cpu", compute_type=ct, cpu_threads=cpu_threads)
    logger.info("Transcription model %s loaded on CPU (cpu_threads=%d)", model_id, cpu_threads)
    return model


def _maybe_retry_gpu_locked() -> None:
    """Under _lock: after _GPU_RETRY_COOLDOWN_S on CPU, drop the models so
    the next load tries the GPU again."""
    global _generic_model, _english_atc_model, _active_config
    if not _force_cpu or _force_cpu_since is None:
        return
    if time.monotonic() - _force_cpu_since < _GPU_RETRY_COOLDOWN_S:
        return
    logger.info("Transcription: cooldown elapsed - retrying GPU.")
    _set_force_cpu(False)
    _generic_model = None
    _english_atc_model = None
    _active_config = None


def unload_models() -> None:
    """Drops the loaded models (and their open files) - before deleting them."""
    global _generic_model, _english_atc_model, _active_config
    with _lock:
        _generic_model = _english_atc_model = _active_config = None
    import gc
    gc.collect()


def _get_models(config: TranscriptionConfig):
    """Lazily loads the model(s) (shared across subsequent calls from the worker thread). The
    second one is None unless an English model is configured."""
    global _generic_model, _english_atc_model, _active_config
    with _lock:
        _maybe_retry_gpu_locked()
        if _active_config != config or _generic_model is None:
            _generic_model = _load_model(config.generic_model, config)
            _english_atc_model = _load_model(config.english_atc_model, config) if config.english_atc_model else None
            _active_config = config
        return _generic_model, _english_atc_model


def _resolve_language(lang: str, prob: float, all_probs, candidates: list) -> tuple:
    """On noisy VHF clips Whisper can pick a random language (Norwegian,
    Finnish...). Prefer an expected language when its probability is close."""
    if lang in candidates:
        return lang, prob
    probs = dict(all_probs)
    best_candidate = max(candidates, key=lambda l: probs.get(l, 0.0))
    best_candidate_prob = probs.get(best_candidate, 0.0)
    if best_candidate_prob >= prob * 0.6:
        return best_candidate, best_candidate_prob
    return lang, prob


def _classify_status(segments, lang_probability: float, duration_after_vad: float) -> str:
    if not segments or duration_after_vad < 0.3:
        return "no_speech"
    avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
    avg_no_speech = sum(s.no_speech_prob for s in segments) / len(segments)
    max_compression = max(s.compression_ratio for s in segments)
    if (
        lang_probability < _LOW_LANG_PROBABILITY
        or avg_logprob < _LOW_AVG_LOGPROB
        or avg_no_speech > _HIGH_NO_SPEECH_PROB
        or max_compression > _HIGH_COMPRESSION_RATIO
    ):
        return "low_confidence"
    return "ok"


class ModelUnavailable(RuntimeError):
    """The Whisper models can't be loaded right now (still downloading, a
    cache being rebuilt, a broken install) - not a problem of the recording,
    so the caller keeps it queued and retries later instead of storing an
    "error" result for every file in the queue."""


def transcribe_recording(
    rec: Recording, config: TranscriptionConfig, _recovery_step: int = 0,
    forced_language: Optional[str] = None,
) -> TranscriptionResult:
    """Transcribes one recording. Errors come back as an "error" result so
    one bad file doesn't stop the queue - except ModelUnavailable (the
    recording is retried later).

    ``forced_language`` ("pl", "en"...) skips detection - a manual
    re-transcription with a chosen language."""
    now = datetime.now(timezone.utc).isoformat()

    if rec.duration_sec and rec.duration_sec < config.min_duration_sec:
        return TranscriptionResult(
            text="", language="", language_probability=0.0,
            status="skipped_short", engine="", transcribed_at=now,
        )

    try:
        from faster_whisper.audio import decode_audio

        try:
            generic_model, english_atc_model = _get_models(config)
        except Exception as exc:  # _load_model already tried GPU -> CPU
            raise ModelUnavailable(str(exc)) from exc
        audio = decode_audio(str(rec.path))

        if forced_language:
            lang, prob = forced_language, 1.0
        else:
            raw_lang, raw_prob, all_probs = generic_model.detect_language(audio=audio, vad_filter=True)
            lang, prob = _resolve_language(raw_lang, raw_prob, all_probs, config.candidate_languages)

        if lang == "en" and english_atc_model is not None:
            model, engine_id = english_atc_model, config.english_atc_model
        else:
            model, engine_id = generic_model, config.generic_model

        segments, info = model.transcribe(
            audio,
            language=lang,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        segments = list(segments)
        text = " ".join(s.text.strip() for s in segments).strip()
        status = _classify_status(segments, prob, info.duration_after_vad)

        return TranscriptionResult(
            text=text, language=lang, language_probability=prob,
            status=status, engine=engine_id, transcribed_at=now,
        )
    except ModelUnavailable:
        raise
    except Exception as exc:
        global _gpu_compute_type, _generic_model, _english_atc_model, _active_config
        if not _force_cpu and _is_cuda_error(exc):
            # First reset both models and try lighter GPU weights. The reset
            # is necessary: the float16 model may have claimed VRAM before
            # the error surfaced on the actual inference call.
            if config.compute_type == "auto" and _gpu_compute_type_for(config) == "float16" and _recovery_step == 0:
                logger.warning(
                    "Transcription: GPU inference failed in float16 (%s); "
                    "retrying on GPU in int8_float16.", exc,
                )
                with _lock:
                    _gpu_compute_type = "int8_float16"
                    _generic_model = None
                    _english_atc_model = None
                    _active_config = None
                return transcribe_recording(rec, config, _recovery_step=1, forced_language=forced_language)

            # A second GPU failure falls back to CPU, until _GPU_RETRY_COOLDOWN_S.
            if _recovery_step < 2:
                logger.warning(
                    "Transcription: GPU doesn't work even after retrying (%s) - "
                    "falling back to CPU (will try GPU again in ~%.0f min).",
                    exc, _GPU_RETRY_COOLDOWN_S / 60,
                )
                with _lock:
                    _set_force_cpu(True)
                    _generic_model = None
                    _english_atc_model = None
                    _active_config = None
                return transcribe_recording(rec, config, _recovery_step=2, forced_language=forced_language)
        logger.exception("Transcription failed for %s", rec.path)
        return TranscriptionResult(
            text="", language="", language_probability=0.0,
            status="error", engine="", transcribed_at=now,
        )
