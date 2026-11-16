"""Where decoded messages go: a ring in memory for the UI and one JSON-lines file per day on disk."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import date, timedelta
from pathlib import Path


class EventHub:
    def __init__(self, directory: Path | None, keep_days: int = 7, ring: int = 3000, stats=None):
        self._dir = directory
        self.stats = stats          # StatsStore: every event is counted there, link frames too
        self.on_event = None        # called with every message worth reading (not link frames): the Notifier
        self._keep_days = keep_days
        self._lock = threading.Lock()
        self._ring: deque[dict] = deque(maxlen=ring)
        self._link_ring: deque[dict] = deque(maxlen=600)    # link-layer frames flood the ring: they get a small one of their own
        self._next_id = 1
        self._pruned_on: date | None = None
        self._load_today()

    def _load_today(self) -> None:
        if self._dir is None:
            return
        path = self._dir / f"events-{date.today().isoformat()}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-self._ring.maxlen:]
        except OSError:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            event["id"] = self._next_id
            self._next_id += 1
            self._ring.append(event)

    def publish(self, addon: str, event: dict, summary: str) -> dict:
        event = {**event, "addon": addon, "ts": round(time.time(), 3), "summary": summary}
        if self.stats is not None:
            self.stats.record(event)
        # Link-layer frames (VDL2 acknowledgements and polls) are most of the traffic and nobody reads them: they are
        # counted in the statistics but neither kept on disk nor shown beyond the live ring.
        persist = not (event.get("kind") == "vdl2" and event.get("layer") == "link")
        with self._lock:
            event["id"] = self._next_id
            self._next_id += 1
            (self._ring if persist else self._link_ring).append(event)
        if persist:
            self._append(event)
            if self.on_event is not None:
                try:
                    self.on_event(event)
                except Exception:  # noqa: BLE001 - a listener must never stop a decoder
                    pass
        return event

    def since(self, after_id: int = 0, addon: str = "", limit: int = 500) -> list[dict]:
        with self._lock:
            rows = [e for e in (*self._ring, *self._link_ring) if e["id"] > after_id and (not addon or e["addon"] == addon)]
        rows.sort(key=lambda e: e["id"])
        return rows[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._ring.clear()
            self._link_ring.clear()

    def _append(self, event: dict) -> None:
        if self._dir is None:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._dir / f"events-{date.today().isoformat()}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({k: v for k, v in event.items() if k != "id"}, ensure_ascii=False) + "\n")
            self._prune()
        except OSError:
            pass     # a full disk must not stop the decoder

    def _prune(self) -> None:
        today = date.today()
        if self._pruned_on == today or self._dir is None:
            return
        self._pruned_on = today
        oldest = today - timedelta(days=self._keep_days)
        for path in self._dir.glob("events-*.jsonl"):
            try:
                if date.fromisoformat(path.stem[7:]) < oldest:
                    path.unlink()
            except (ValueError, OSError):
                pass
