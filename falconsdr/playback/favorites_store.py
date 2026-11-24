"""Persistent set of favorite recordings - keyed by `cache_key()`
(relpath+size+mtime), with relative-path fallback so a file copy, path move,
or upgrade that shifts mtime or path prefix never silently drops a user's stars."""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Set

from falconsdr.config import atomic_write_text, quarantine
from falconsdr.playback.config import FAVORITES_FILE

logger = logging.getLogger(__name__)

# Extracts `YYYY-MM-DD/<filename>` even from an old absolute path (Linux or Windows).
_DATE_TAIL_RE = re.compile(r"(?:^|/)(\d{4}-\d{2}-\d{2}/[^/]+)$")


def _rel_from_key(key: str) -> str:
    """Normalizes a cache key (`path|size|mtime`) to its relative path below the
    library root, ignoring absolute prefixes, backslash separators, and mtime."""
    raw = key.split("|", 1)[0].replace("\\", "/").strip("/")
    m = _DATE_TAIL_RE.search(raw)
    if m:
        return m.group(1)
    # Legacy loose file with an absolute path: match by filename alone.
    if "/" in raw and (raw.startswith(("home/", "var/", "opt/", "mnt/", "media/", "Users/", "srv/")) or ":" in raw.split("/", 1)[0]):
        return raw.rsplit("/", 1)[-1]
    return raw


class FavoritesStore:
    def __init__(self, favorites_file: Path = FAVORITES_FILE):
        self.favorites_file = favorites_file
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._keys: Set[str] = set()
        self._by_rel: dict[str, str] = {}
        self._changed = False
        self._load()

    def _rebuild_index(self) -> None:
        self._by_rel = {_rel_from_key(k): k for k in self._keys}

    def _load(self) -> None:
        if self.favorites_file.exists():
            try:
                raw = json.loads(self.favorites_file.read_text(encoding="utf-8"))
                if not isinstance(raw, list):
                    raise ValueError("favorites file is not a JSON list")
                self._keys = {str(k) for k in raw}
                self._rebuild_index()
            except (ValueError, OSError) as exc:
                logger.warning("Failed to load the favorites list, starting fresh")
                if isinstance(exc, ValueError):
                    quarantine(self.favorites_file)
                self._keys = set()
                self._by_rel = {}

    @property
    def changed(self) -> bool:
        with self._lock:
            return self._changed

    def save(self) -> None:
        with self._save_lock:
            with self._lock:
                snapshot = sorted(self._keys)
                self._changed = False
            try:
                atomic_write_text(self.favorites_file, json.dumps(snapshot))
            except OSError as exc:
                logger.warning("Failed to save the favorites list: %s", exc)

    def is_favorite(self, key: str) -> bool:
        with self._lock:
            if key in self._keys:
                return True
            rel = _rel_from_key(key)
            old = self._by_rel.get(rel)
            if old is None:
                return False
            # Same recording, shifted mtime/size or legacy path prefix: preserve the star and upgrade its key.
            self._keys.discard(old)
            self._keys.add(key)
            self._by_rel[rel] = key
            self._changed = True
            return True

    def set_favorite(self, key: str, favorite: bool) -> None:
        """In memory only - the caller saves once after a batch (starring 5,000
        recordings must not rewrite the whole file 5,000 times)."""
        rel = _rel_from_key(key)
        with self._lock:
            old = self._by_rel.get(rel)
            if favorite:
                if old is not None and old != key:
                    self._keys.discard(old)
                self._keys.add(key)
                self._by_rel[rel] = key
            else:
                self._keys.discard(key)
                if old is not None:
                    self._keys.discard(old)
                self._by_rel.pop(rel, None)
            self._changed = True

    def prune(self, valid_keys: set[str]) -> int:
        """Forgets stars of files that no longer exist; returns how many (caller saves)."""
        valid_rels = {_rel_from_key(k) for k in valid_keys}
        with self._lock:
            stale = {k for k in self._keys if k not in valid_keys and _rel_from_key(k) not in valid_rels}
            if stale:
                self._keys -= stale
                self._rebuild_index()
                self._changed = True
            return len(stale)

    def remove(self, key: str) -> None:
        """Removes a single entry (e.g. right after the user manually deletes a recording); caller saves."""
        self.set_favorite(key, False)
