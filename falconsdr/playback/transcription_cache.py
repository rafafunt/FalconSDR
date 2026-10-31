"""Persistent cache of transcription results, so recordings that were
already processed once don't get run through Whisper again (transcription is
many times slower than quality analysis, so caching matters even more here
than in QualityCache)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from falconsdr.config import atomic_write_text, dumps_chunked, quarantine
from falconsdr.playback.config import TRANSCRIPTION_CACHE_FILE
from falconsdr.playback.models import TranscriptionResult

logger = logging.getLogger(__name__)


class TranscriptionCache:
    def __init__(self, cache_file: Path = TRANSCRIPTION_CACHE_FILE):
        self.cache_file = cache_file
        self._data: Dict[str, dict] = {}
        self._version = 0        # bumped by every change
        self._saved_version = 0  # ...and what the file on disk has
        self._load()

    def _load(self) -> None:
        if self.cache_file.exists():
            try:
                self._data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                logger.warning("Failed to load the transcription cache, starting fresh")
                if isinstance(exc, ValueError):
                    quarantine(self.cache_file)
                self._data = {}

    @property
    def changed(self) -> bool:
        return self._version != self._saved_version

    def mark_changed(self) -> None:
        """For code that edits ``_data`` directly."""
        self._version += 1

    def save(self) -> None:
        """Writes the file if anything changed since the last write (46k entries take ~0.3 s of CPU to encode)."""
        if not self.changed:
            return
        version = self._version
        try:
            atomic_write_text(self.cache_file, dumps_chunked(self._data, ensure_ascii=False))
            self._saved_version = version
        except OSError as exc:
            logger.warning("Failed to save the transcription cache: %s", exc)

    def get(self, key: str) -> TranscriptionResult | None:
        raw = self._data.get(key)
        if raw is None:
            return None
        return TranscriptionResult(**raw)

    def put(self, key: str, result: TranscriptionResult) -> None:
        self._version += 1
        self._data[key] = {
            "text": result.text,
            "language": result.language,
            "language_probability": result.language_probability,
            "status": result.status,
            "engine": result.engine,
            "transcribed_at": result.transcribed_at,
        }

    def prune(self, valid_keys: set[str]) -> int:
        """Removes cache entries for files that no longer exist (moved/deleted); returns how many."""
        stale = set(self._data) - valid_keys
        for key in stale:
            del self._data[key]
        if stale:
            self._version += 1
        return len(stale)

    def remove(self, key: str) -> None:
        """Removes a single entry (e.g. right after the user manually deletes a recording)."""
        if self._data.pop(key, None) is not None:
            self._version += 1
