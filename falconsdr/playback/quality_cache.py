"""Persistent cache of quality-analysis results, so it doesn't get
recomputed on every program launch for recordings that haven't changed."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from falconsdr.config import atomic_write_text, dumps_chunked, quarantine
from falconsdr.playback.config import QUALITY_CACHE_FILE
from falconsdr.playback.models import QualityMetrics

logger = logging.getLogger(__name__)


class QualityCache:
    def __init__(self, cache_file: Path = QUALITY_CACHE_FILE):
        self.cache_file = cache_file
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.cache_file.exists():
            try:
                self._data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                logger.warning("Failed to load the quality cache, starting fresh")
                if isinstance(exc, ValueError):
                    quarantine(self.cache_file)
                self._data = {}

    def save(self) -> None:
        try:
            atomic_write_text(self.cache_file, dumps_chunked(self._data))
        except OSError as exc:
            logger.warning("Failed to save the quality cache: %s", exc)

    def get(self, key: str) -> QualityMetrics | None:
        raw = self._data.get(key)
        if raw is None:
            return None
        return QualityMetrics(**raw)

    def put(self, key: str, quality: QualityMetrics) -> None:
        self._data[key] = {
            "noise_floor": quality.noise_floor,
            "signal_level": quality.signal_level,
            "snr_db": quality.snr_db,
            "clipping_ratio": quality.clipping_ratio,
            "quality_score": quality.quality_score,
            "duration_sec": quality.duration_sec,
            "readable": quality.readable,
        }

    def prune(self, valid_keys: set[str]) -> None:
        """Removes cache entries for files that no longer exist (moved/deleted)."""
        stale = set(self._data) - valid_keys
        for key in stale:
            del self._data[key]

    def remove(self, key: str) -> None:
        """Removes a single entry (e.g. right after the user manually deletes a recording)."""
        self._data.pop(key, None)
