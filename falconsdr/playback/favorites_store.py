"""Persistent set of favorite recordings - the same `cache_key()`
(path+size+mtime) as the other caches in this module, so one file just holds
keys (a star is a binary fact, there's nothing to recompute like in the
quality cache)."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Set

from falconsdr.config import atomic_write_text, quarantine
from falconsdr.playback.config import FAVORITES_FILE

logger = logging.getLogger(__name__)


class FavoritesStore:
    def __init__(self, favorites_file: Path = FAVORITES_FILE):
        self.favorites_file = favorites_file
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._keys: Set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.favorites_file.exists():
            try:
                raw = json.loads(self.favorites_file.read_text(encoding="utf-8"))
                if not isinstance(raw, list):
                    raise ValueError("favorites file is not a JSON list")
                self._keys = {str(k) for k in raw}
            except (ValueError, OSError) as exc:
                logger.warning("Failed to load the favorites list, starting fresh")
                if isinstance(exc, ValueError):
                    quarantine(self.favorites_file)
                self._keys = set()

    def save(self) -> None:
        with self._save_lock:
            with self._lock:
                snapshot = sorted(self._keys)
            try:
                atomic_write_text(self.favorites_file, json.dumps(snapshot))
            except OSError as exc:
                logger.warning("Failed to save the favorites list: %s", exc)

    def is_favorite(self, key: str) -> bool:
        with self._lock:
            return key in self._keys

    def set_favorite(self, key: str, favorite: bool) -> None:
        """In memory only - the caller saves once after a batch (starring 5,000
        recordings must not rewrite the whole file 5,000 times)."""
        with self._lock:
            if favorite:
                self._keys.add(key)
            else:
                self._keys.discard(key)

    def prune(self, valid_keys: set[str]) -> int:
        """Forgets stars of files that no longer exist; returns how many (caller saves)."""
        with self._lock:
            stale = self._keys - valid_keys
            self._keys -= stale
            return len(stale)

    def remove(self, key: str) -> None:
        """Removes a single entry (e.g. right after the user manually deletes a recording); caller saves."""
        with self._lock:
            self._keys.discard(key)
