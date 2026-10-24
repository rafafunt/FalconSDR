"""Time-of-day scan schedule (ScheduleConfig): which bands and whether the
Memory List are scanned right now. Times are the receiver's local time."""
from __future__ import annotations

import datetime as dt

from .config import AppConfig, ScheduleEntry, VfoRangeConfig


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_active(entry: ScheduleEntry, now: dt.datetime) -> bool:
    try:
        start, end = _minutes(entry.start), _minutes(entry.end)
    except ValueError:
        return False
    t, day = now.hour * 60 + now.minute, now.weekday()
    if start == end:
        return day in entry.days
    if start < end:
        return day in entry.days and start <= t < end
    return (t >= start and day in entry.days) or (t < end and (day - 1) % 7 in entry.days)


def active_indices(config: AppConfig, now: dt.datetime | None = None) -> list[int]:
    now = now or dt.datetime.now()
    return [i for i, e in enumerate(config.schedule.entries) if is_active(e, now)]


def scan_selection(config: AppConfig, now: dt.datetime | None = None) -> tuple[list[VfoRangeConfig], bool]:
    """(bands to scan, Memory List on) - the union of the active entries when
    the schedule is on, else (and between entries with outside="normal") each
    band's and the Memory List's own switch."""
    entries = [config.schedule.entries[i] for i in active_indices(config, now)] if config.schedule.enabled else []
    if not entries and (not config.schedule.enabled or config.schedule.outside == "normal"):
        return [b for b in config.bands if b.enabled], config.memory_list_enabled
    ids = {i for e in entries for i in e.band_ids}
    return [b for b in config.bands if b.id in ids], any(e.memory_list for e in entries)


def signature(config: AppConfig) -> tuple:
    """Changes exactly when scan_selection()'s result can change by itself (time passing)."""
    if not config.schedule.enabled:
        return (False, "", ())
    return (True, config.schedule.outside, tuple(active_indices(config)))
