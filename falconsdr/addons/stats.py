"""Statistics of what the add-ons decoded: one small bucket per hour and add-on, kept for months (the messages
themselves are kept for days). Counts, who and which channel, and a few extremes - never the text."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

RETENTION_DAYS = 90
_FLUSH_EVERY_S = 30.0
_MAX_WHO_PER_HOUR = 300           # aircraft/stations remembered per hour; the rest only count


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _who(event: dict) -> str:
    if event.get("kind") == "sensor":
        return f"{event.get('model', '?')} #{event.get('device_id')}"
    return str(event.get("registration") or event.get("source") or "")


class StatsStore:
    def __init__(self, directory: Path | None, home: Callable[[], tuple[float, float]] = lambda: (0.0, 0.0)):
        self._dir = directory
        self._home = home
        self._lock = threading.Lock()
        self._hours: dict[int, dict[str, dict]] = {}     # hour start (epoch) -> add-on -> bucket
        self._dirty: set[str] = set()                     # UTC days to write
        self._last_flush = time.monotonic()
        self._load()

    # -- persistence --------------------------------------------------------------------------------------
    def _day(self, hour: int) -> str:
        return datetime.fromtimestamp(hour, timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> None:
        if self._dir is None or not self._dir.is_dir():
            return
        oldest = datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS)
        for path in sorted(self._dir.glob("stats-*.json")):
            try:
                if datetime.strptime(path.stem[6:], "%Y-%m-%d").date() < oldest:
                    path.unlink()
                    continue
                for hour, buckets in json.loads(path.read_text(encoding="utf-8")).items():
                    for bucket in buckets.values():
                        for sensor in bucket.get("sensors", {}).values():
                            if "vn" not in sensor:      # written before sensors were classified: it only knew temperature
                                sensor["vn"], sensor["vsum"] = sensor.get("tn", 0), sensor.get("tsum", 0.0)
                                if "tmin" in sensor:
                                    sensor["vmin"], sensor["vmax"], sensor["pl"], sensor["pu"] = sensor["tmin"], sensor["tmax"], "Temperature", "°C"
                                sensor.setdefault("cat", "thermometer")
                    self._hours[int(hour)] = buckets
            except (ValueError, OSError):
                continue

    def flush(self, force: bool = False) -> None:
        if self._dir is None:
            return
        with self._lock:
            if not self._dirty or (not force and time.monotonic() - self._last_flush < _FLUSH_EVERY_S):
                return
            days, self._dirty = self._dirty, set()
            self._last_flush = time.monotonic()
            payload = {d: {str(h): b for h, b in self._hours.items() if self._day(h) == d} for d in days}
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            for day, data in payload.items():
                tmp = self._dir / f".stats-{day}.tmp"
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self._dir / f"stats-{day}.json")
        except OSError:
            pass        # a full disk must not stop the decoder

    @property
    def empty(self) -> bool:
        return not self._hours

    def backfill(self, events_dir: Path) -> int:
        """First start with statistics: count what the stored messages already say (the days kept on disk)."""
        n = 0
        for path in sorted(events_dir.glob("events-*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    self.record(json.loads(line))
                    n += 1
                except ValueError:
                    continue
        self.flush(force=True)
        return n

    # -- recording ------------------------------------------------------------------------------------------
    def record(self, event: dict) -> None:
        ts = float(event.get("ts") or time.time())
        hour = int(ts // 3600) * 3600
        addon = str(event.get("addon", ""))
        with self._lock:
            b = self._hours.setdefault(hour, {}).setdefault(addon, {"n": 0, "link": 0, "last": 0.0, "freq": {}, "lvl": {}, "who": {},
                                                                    "labels": {}, "types": {}, "pos": 0, "sensors": {}})
            b["last"] = max(b["last"], ts)
            if event.get("kind") == "vdl2" and event.get("layer") == "link":
                b["link"] += 1                 # acknowledgements and polls: counted, not "messages"
            else:
                b["n"] += 1
                self._count(b, event, ts)
            self._dirty.add(self._day(hour))
        self.flush()

    def _count(self, b: dict, event: dict, ts: float) -> None:
        freq = event.get("freq_hz")
        if freq:
            key = str(int(freq))
            b["freq"][key] = b["freq"].get(key, 0) + 1
            level = event.get("sig_level")
            if isinstance(level, (int, float)):
                s = b["lvl"].setdefault(key, [0.0, 0])
                s[0] += level
                s[1] += 1
        who = _who(event)
        if who and (who in b["who"] or len(b["who"]) < _MAX_WHO_PER_HOUR):
            w = b["who"].setdefault(who, [0, 0.0])
            w[0] += 1
            w[1] = max(w[1], ts)
        label = event.get("label")
        if label:
            b["labels"][str(label)] = b["labels"].get(str(label), 0) + 1
        if event.get("kind") == "aprs":
            kind = str(event.get("type") or "?")
            b["types"][kind] = b["types"].get(kind, 0) + 1
            if "lat" in event and "lon" in event:
                b["pos"] += 1
                home = self._home()
                if home[0] or home[1]:
                    km = _haversine_km(home[0], home[1], float(event["lat"]), float(event["lon"]))
                    if km > b.get("dist", 0.0):
                        b["dist"], b["dist_who"] = round(km, 1), str(event.get("source", ""))
        if event.get("kind") == "sensor":
            s = b["sensors"].setdefault(who, {"n": 0, "last": 0.0, "vn": 0, "vsum": 0.0})
            s["n"] += 1
            s["last"] = max(s["last"], ts)
            s["cat"] = event.get("category", "other")
            main = next((r for r in event.get("readings", []) if r["key"] == event.get("primary")), None)
            if main is not None:
                s["pl"], s["pu"] = main["label"], main["unit"]
                v = main["value"]
                s["vmin"], s["vmax"] = min(s.get("vmin", v), v), max(s.get("vmax", v), v)
                s["vn"] += 1
                s["vsum"] += v
            batt = (event.get("fields") or {}).get("battery_ok")
            if batt is not None:
                s["batt"] = 1 if batt else 0

    # -- reading --------------------------------------------------------------------------------------------
    def snapshot(self, days: int, now: float | None = None) -> dict:
        now = now or time.time()
        since = now - days * 86400 if days else 0
        with self._lock:
            hours = {h: b for h, b in self._hours.items() if h + 3600 > since}
            available = len({self._day(h) for h in self._hours})
        out: dict[str, dict] = {}
        for hour in sorted(hours):
            for addon, b in hours[hour].items():
                a = out.setdefault(addon, {"n": 0, "link": 0, "last": 0.0, "timeline": [], "who": {}, "freq": {}, "lvl": {}, "labels": {},
                                           "types": {}, "pos": 0, "farthest": None, "sensors": {}})
                a["n"] += b["n"]
                a["link"] += b["link"]
                a["last"] = max(a["last"], b["last"])
                a["pos"] += b.get("pos", 0)
                a["timeline"].append([hour, b["n"], b["link"]])
                for name, (n, last) in b["who"].items():
                    w = a["who"].setdefault(name, [0, 0.0])
                    w[0] += n
                    w[1] = max(w[1], last)
                for key in ("freq", "labels", "types"):
                    for k, n in b[key].items():
                        a[key][k] = a[key].get(k, 0) + n
                for k, (s, n) in b["lvl"].items():
                    t = a["lvl"].setdefault(k, [0.0, 0])
                    t[0] += s
                    t[1] += n
                if b.get("dist") and (a["farthest"] is None or b["dist"] > a["farthest"]["km"]):
                    a["farthest"] = {"km": b["dist"], "who": b.get("dist_who", "")}
                for name, s in b["sensors"].items():
                    t = a["sensors"].setdefault(name, {"n": 0, "vn": 0, "vsum": 0.0, "last": 0.0})
                    t["n"] += s["n"]; t["vn"] += s["vn"]; t["vsum"] += s["vsum"]; t["last"] = max(t["last"], s["last"])
                    if s["last"] >= t.get("seen_at", 0):
                        t["seen_at"] = s["last"]
                        for k in ("cat", "pl", "pu"):
                            if k in s:
                                t[k] = s[k]
                    if "vmin" in s:
                        t["vmin"], t["vmax"] = min(t.get("vmin", s["vmin"]), s["vmin"]), max(t.get("vmax", s["vmax"]), s["vmax"])
                    if "batt" in s and s["last"] >= t.get("batt_at", 0):
                        t["batt"], t["batt_at"] = s["batt"], s["last"]
        for a in out.values():
            a["who"] = [[n, c, last] for n, (c, last) in sorted(a["who"].items(), key=lambda kv: -kv[1][0])[:60]]
            a["distinct"] = len(a["who"])
            a["freq"] = [[int(f), n, round(a["lvl"][f][0] / a["lvl"][f][1], 1) if f in a["lvl"] else None]
                         for f, n in sorted(a["freq"].items(), key=lambda kv: -kv[1])]
            del a["lvl"]
            a["labels"] = sorted(a["labels"].items(), key=lambda kv: -kv[1])[:20]
            a["types"] = sorted(a["types"].items(), key=lambda kv: -kv[1])
            a["sensors"] = [{"device": name, "n": s["n"], "last": s["last"], "category": s.get("cat", "other"), "label": s.get("pl", ""), "unit": s.get("pu", ""),
                             "vmin": s.get("vmin"), "vmax": s.get("vmax"), "vavg": round(s["vsum"] / s["vn"], 2) if s["vn"] else None,
                             "batt": s.get("batt")} for name, s in sorted(a["sensors"].items(), key=lambda kv: -kv[1]["n"])]
        return {"days": days, "days_available": available, "addons": out}
