"""CSV persistence for the memory-list channels and the blacklist (spec 4.5)."""
from __future__ import annotations

import csv
import io
import logging

from .config import BLACKLIST_CSV, CHANNELS_CSV, atomic_write_text
from .models import BlacklistEntry, Channel

logger = logging.getLogger(__name__)


def load_channels() -> list[Channel]:
    if not CHANNELS_CSV.exists():
        return []
    with CHANNELS_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if rows and rows[0][:1] == ["freq_hz"]:
        rows = rows[1:]
    channels = []
    for row in rows:
        if not row:
            continue
        try:
            channels.append(Channel.from_row(row))
        except (ValueError, IndexError) as exc:
            logger.warning("Skipping corrupted row in %s: %r (%s)", CHANNELS_CSV, row, exc)
    return channels


def save_channels(channels: list[Channel]) -> None:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["freq_hz", "name", "modulation", "enabled", "folder", "weight", "ch833", "overrides"])
    for ch in channels:
        writer.writerow(ch.to_row())
    atomic_write_text(CHANNELS_CSV, buf.getvalue())


def load_blacklist() -> list[BlacklistEntry]:
    if not BLACKLIST_CSV.exists():
        return []
    with BLACKLIST_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if rows and rows[0][:1] == ["freq_hz"]:
        rows = rows[1:]
    entries = []
    for row in rows:
        if not row:
            continue
        try:
            entries.append(BlacklistEntry.from_row(row))
        except (ValueError, IndexError) as exc:
            logger.warning("Skipping corrupted row in %s: %r (%s)", BLACKLIST_CSV, row, exc)
    return entries


def save_blacklist(entries: list[BlacklistEntry]) -> None:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["freq_hz", "margin_hz", "label", "date_added"])
    for e in entries:
        writer.writerow(e.to_row())
    atomic_write_text(BLACKLIST_CSV, buf.getvalue())
