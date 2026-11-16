"""Time-of-day scan schedule (ScheduleConfig): which bands and whether the
Memory List are scanned right now. Times are the receiver's local time."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Iterable

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


# -- several receivers and add-ons ---------------------------------------------------------------------------

@dataclass
class Plan:
    """What one receiver does right now."""
    addon: str = ""                                   # an external add-on holds it (it scans nothing)
    bands: list[VfoRangeConfig] = field(default_factory=list)
    memory: bool = False
    group: tuple[int, ...] = ()                       # the receivers that share this plan (this one among them)
    entries: tuple[int, ...] = ()                     # the active schedule entries behind it
    held: bool = False                                # held by an add-on someone started by hand


def assignments(config: AppConfig, count: int, held: Iterable[int] = (), now: dt.datetime | None = None,
                addon_receiver: Callable[[str], int] = lambda _a: 0, addon_ok: Callable[[str], bool] = lambda _a: True) -> list[Plan]:
    """The plan of every receiver ``0..count-1`` at ``now``.

    * An add-on entry (``entry.addon``) holds its receivers - the ones it names, else ``addon_receiver(addon)`` - and the
      first entry wins when two want the same receiver. ``addon_ok`` says whether it can run at all (installed, not
      failed): one that cannot is ignored, so the receiver is not wasted.
    * A scan entry that names receivers is theirs alone: such a receiver scans only its own active entries.
    * Entries naming no receiver are shared: the receivers without entries of their own split the union of them.
    * With nothing active for a receiver, ``outside`` decides: "off" = nothing, "normal" = each band's own switch.
    * With the schedule off, every free receiver splits the usual selection. Receivers held by add-ons never scan."""
    held = set(held)
    sched = config.schedule
    plans = [Plan(held=k in held) for k in range(count)]
    active = active_indices(config, now) if sched.enabled else []
    usual = ([b for b in config.bands if b.enabled], config.memory_list_enabled)

    # 1. add-ons take their receivers
    for i in active:
        e = sched.entries[i]
        if not e.addon or not addon_ok(e.addon):
            continue
        for k in (e.receivers[:1] or [addon_receiver(e.addon)]):          # one program runs on one receiver
            if 0 <= k < count and not plans[k].addon and k not in held:
                plans[k].addon, plans[k].entries = e.addon, (i,)
    free = [k for k in range(count) if not plans[k].addon and k not in held]

    def selection(indices: Iterable[int]):
        chosen = [sched.entries[i] for i in indices]
        ids = {b for e in chosen for b in e.band_ids}
        return [b for b in config.bands if b.id in ids], any(e.memory_list for e in chosen)

    if not sched.enabled:
        for k in free:
            plans[k].bands, plans[k].memory, plans[k].group = usual[0], usual[1], tuple(free)
        return plans

    scans = [i for i in active if not sched.entries[i].addon]
    shared = [i for i in scans if not sched.entries[i].receivers]
    own = {k: tuple(i for i in scans if k in sched.entries[i].receivers) for k in free}
    for k in free:                                    # receivers with entries of their own
        if own[k]:
            same = tuple(j for j in free if own[j] == own[k])
            plans[k].bands, plans[k].memory = selection(own[k])
            plans[k].group, plans[k].entries = same, own[k]
    rest = [k for k in free if not own[k]]            # the others follow the shared entries, or "outside"
    if shared:
        bands, memory = selection(shared)
    elif sched.outside == "normal":
        bands, memory = usual
    else:
        bands, memory = [], False
    for k in rest:
        plans[k].bands, plans[k].memory, plans[k].group, plans[k].entries = bands, memory, tuple(rest), tuple(shared)
    return plans


def plan_signature(plans: list[Plan]) -> tuple:
    """Changes exactly when a plan can change by itself (time passing, an add-on starting)."""
    return tuple((p.addon, p.entries, p.group, p.held) for p in plans)


def union_selection(plans: list[Plan]) -> tuple[list[VfoRangeConfig], bool]:
    """Everything any receiver scans now (for "what is not covered")."""
    seen: dict[str, VfoRangeConfig] = {}
    for p in plans:
        for b in p.bands:
            seen.setdefault(b.id, b)
    return list(seen.values()), any(p.memory for p in plans)
