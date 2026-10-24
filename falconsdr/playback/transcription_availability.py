"""Whether faster-whisper is importable right now - the one thing the whole
optional-transcription paths (worker.py, the Settings page,
TranscriptionWorker's guard) needs to agree on, so it lives in one place
instead of every caller re-implementing its own check.
"""
from __future__ import annotations

import importlib
import importlib.util


def is_transcription_installed() -> bool:
    """``find_spec``, not an import - cheap. ``invalidate_caches()`` first,
    or a package pip installed after startup stays invisible."""
    importlib.invalidate_caches()
    return importlib.util.find_spec("faster_whisper") is not None
