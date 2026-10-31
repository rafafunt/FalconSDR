"""How several receivers share one scan plan (gotcha 46): antenna ranges and capture widths.

Someone with several dongles on different antennas - say a 2 m one and a
70 cm one - sets each dongle's range, and a dongle then only scans inside it.
No limit (the default) keeps the old behaviour: every dongle scans anything
and the scan plan is shared between them (scanner_engine._partition_windows).

Receivers of different kinds (an RTL-SDR at 2.4 MHz next to a HackRF at 10 MHz) have different capture
widths. Every engine then builds the plan for the NARROWEST width - each window fits every receiver -
hands the windows out in proportion to the widths (``split_by_range(weights=...)``) and joins the ones it
got into windows of its own width (``widen_windows``). Engines agree without talking because all of them
compute the same thing from the same list of widths.
"""
from __future__ import annotations

from typing import Iterable

from .channelizer import USABLE_FRACTION, ScanWindow
from .config import AppConfig
from .models import Channel

OPEN_END_HZ = 1 << 40  # "no upper limit"; far above any SDR


def device_range_hz(device) -> tuple[int, int]:
    """(low, high) of one dongle's range; ``device`` is config.device or an
    ``extra_devices`` dict. 0 in the config is an open end; reversed ends are swapped."""
    get = device.get if isinstance(device, dict) else (lambda key, default=0: getattr(device, key, default))
    low = max(0, int(get("range_start_hz", 0) or 0))
    high = int(get("range_stop_hz", 0) or 0)
    high = high if high > 0 else OPEN_END_HZ
    return (high, low) if high < low else (low, high)


def device_ranges(config: AppConfig) -> list[tuple[int, int]]:
    """One range per dongle, in the Controller's engine order (config.device first)."""
    extra = [d for d in config.device.extra_devices if isinstance(d, dict) and d.get("driver")]
    return [device_range_hz(config.device), *(device_range_hz(d) for d in extra)]


def device_rates(config: AppConfig) -> list[float]:
    """Each dongle's wanted capture width in Hz, in engine order: config.device's, and an extra dongle's own
    ``sample_rate_hz`` or else the main one. (What the hardware really captures can differ - Airspy has a few
    fixed rates - and replaces this once the engine has opened it.)"""
    main = float(config.device.sample_rate_hz)
    extra = [d for d in config.device.extra_devices if isinstance(d, dict) and d.get("driver")]
    return [main, *(float(d.get("sample_rate_hz") or main) for d in extra)]


def is_limited(ranges: Iterable[tuple[int, int]]) -> bool:
    return any(low > 0 or high < OPEN_END_HZ for low, high in ranges)


def split_by_range(default_windows: list, lanes: list, index: int, ranges: list[tuple[int, int]],
                   weights: list[float] | None = None):
    """This dongle's share of the scan plan when at least one dongle has a range.

    Every engine runs this on the same plan, so they agree without talking to
    each other: each frequency goes to one of the dongles whose range holds it
    (taking turns, window by window, when several qualify) and to nobody when
    none does. A window that is only partly this dongle's keeps just its
    frequencies, re-centred on them. With ``weights`` (capture widths) the turns
    are proportional - smooth weighted round-robin, which for equal weights is
    exactly the plain alternation. Returns (default windows, lanes)."""
    order = sorted([*default_windows, *(w for lane in lanes for w in lane.windows)], key=lambda w: w.center_hz)
    credit: dict[tuple, dict[int, float]] = {}
    weights = weights or [1.0] * len(ranges)
    mine: dict[int, list[int]] = {}
    for window in order:
        groups: dict[tuple, list[int]] = {}
        for freq in window.freqs:
            who = tuple(k for k, (low, high) in enumerate(ranges) if low <= freq <= high)
            if who:
                groups.setdefault(who, []).append(freq)
        for who, freqs in groups.items():
            # Smooth weighted round-robin (the nginx scheme): the dongle with the most credit takes the turn.
            have = credit.setdefault(who, {k: 0.0 for k in who})
            for k in who:
                have[k] += weights[k]
            chosen = max(who, key=lambda k: (have[k], -k))
            have[chosen] -= sum(weights[k] for k in who)
            if chosen == index:
                mine.setdefault(id(window), []).extend(freqs)

    def restrict(window):
        freqs = sorted(mine.get(id(window), []))
        if not freqs:
            return None
        if len(freqs) == len(window.freqs):
            return window
        return ScanWindow(center_hz=(freqs[0] + freqs[-1]) // 2, freqs=freqs, band=window.band)

    kept = [w for w in map(restrict, default_windows) if w is not None]
    kept_lanes = []
    for lane in lanes:
        windows = [w for w in map(restrict, lane.windows) if w is not None]
        if windows:
            kept_lanes.append(type(lane)(weight=lane.weight, windows=windows, is_range=lane.is_range))
    return kept, kept_lanes


def widen_windows(windows: list, rate_hz: float, channel_bw: dict, default_bw: float = 25_000.0) -> list:
    """Joins neighbouring windows of one band into windows as wide as this receiver captures. The plan was made
    for the narrowest receiver; a wider one would otherwise retune for every little window. Only windows of
    the same band are joined (its overrides stay valid) and only while all their frequencies, with the
    channel's own width, fit the usable part of the capture. Nothing is dropped or duplicated."""
    usable = rate_hz * USABLE_FRACTION
    out: list[ScanWindow] = []
    group: list[ScanWindow] = []

    def bw(freq: int) -> float:
        return float(channel_bw.get(freq, default_bw))

    def flush() -> None:
        if not group:
            return
        if len(group) == 1:
            out.append(group[0])
        else:
            freqs = sorted({f for w in group for f in w.freqs})
            widest = max(bw(f) for f in freqs)
            out.append(ScanWindow(center_hz=(freqs[0] + freqs[-1]) // 2 + int(widest * 0.37), freqs=freqs, band=group[0].band))
        group.clear()

    for window in sorted(windows, key=lambda w: w.center_hz):
        if group and window.band is group[0].band:
            freqs = [f for w in group for f in w.freqs] + list(window.freqs)
            if max(freqs) - min(freqs) + max(bw(f) for f in freqs) <= usable:
                group.append(window)
                continue
        flush()
        group.append(window)
    flush()
    return out


def uncovered(config: AppConfig, channels: list[Channel], bands, memory_on: bool) -> list[dict]:
    """What the user wants scanned that no dongle's range reaches - nothing
    unless every dongle has a range. ``bands``/``memory_on`` are what is being
    scanned now (schedule.scan_selection). Bands come with the uncovered gaps
    ([start, stop] Hz); channels as a count plus a few example frequencies."""
    ranges = device_ranges(config)
    if not is_limited(ranges) or any(low == 0 and high == OPEN_END_HZ for low, high in ranges):
        return []
    merged: list[list[int]] = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])

    def gaps(start: int, stop: int) -> list[list[int]]:
        out, at = [], start
        for low, high in merged:
            if high < at:
                continue
            if low > stop:
                break
            if low > at:
                out.append([at, low - 1])
            at = max(at, high + 1)
        if at <= stop:
            out.append([at, stop])
        return out

    items: list[dict] = []
    for band in bands:
        start, stop = sorted((band.start_hz, band.stop_hz))
        missing = gaps(start, stop)
        if missing:
            items.append({"kind": "band", "label": band.label, "start_hz": start, "stop_hz": stop, "gaps": missing})
    if memory_on:
        outside = [c.freq_hz for c in channels if c.enabled and not any(low <= c.freq_hz <= high for low, high in merged)]
        if outside:
            items.append({"kind": "channels", "count": len(outside), "examples": sorted(outside)[:3]})
    return items
