"""The recording library as the web UI sees it: one in-memory index of every
closed recording under the output folder, with its quality analysis,
transcript, favorite flag and "a computer holds a copy" (mirrored) flag.

Also the server side of the worker protocol (see worker.py): handing out
transcription jobs with a lease, accepting results, importing a client's
existing library metadata - and disk retention.

Files the scanner is still writing are never listed (``open_paths``) - a
listed file may be downloaded, mirrored or deleted, which must never happen
mid-recording.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from falconsdr.config import AppConfig, atomic_write_text
from falconsdr.military_presets import get_all_presets
from falconsdr.models import airband_channel, snap_airband_hz
from falconsdr.playback import stats
from falconsdr.playback.audio_analysis import analyze_wav, compute_waveform_peaks
from falconsdr.playback.config import APP_DATA_DIR, UTC_TZ
from falconsdr.playback.favorites_store import FavoritesStore
from falconsdr.playback.models import MANUAL_FREQ_KHZ, QualityMetrics, Recording, TranscriptionResult
from falconsdr.playback.organizer import organize, parse_filename
from falconsdr.playback.quality_cache import QualityCache
from falconsdr.playback.transcription_cache import TranscriptionCache

logger = logging.getLogger(__name__)

MIRRORED_FILE = APP_DATA_DIR / "mirrored.json"
_LEASE_S = 15 * 60
_RESCAN_INTERVAL_S = 120.0
_RETENTION_INTERVAL_S = 600.0
_SAVE_INTERVAL_S = 5.0


def _from_fields(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


def transcript_dict(result: Optional[TranscriptionResult]) -> Optional[dict]:
    return asdict(result) if result else None


class Library:
    def __init__(
        self,
        config: AppConfig,
        open_paths: Callable[[], set[Path]],
        freq_names: Callable[[], dict[int, str]],
    ) -> None:
        self.config = config
        self._open_paths = open_paths
        self._freq_names = freq_names
        self._lock = threading.RLock()
        self._recs: dict[str, Recording] = {}   # relpath (posix) -> Recording
        self._blobs: dict[str, str] = {}        # relpath -> lowercased search text
        self.quality = QualityCache()
        self.transcripts = TranscriptionCache()
        self.favorites = FavoritesStore()
        self._mirrored: set[str] = self._load_mirrored()
        self._requests: dict[str, Optional[str]] = {}   # relpath -> forced language (None = auto)
        self._leases: dict[str, tuple[str, float]] = {}  # relpath -> (worker, expires monotonic)
        self._failed: set[str] = set()   # errored this session - not re-queued automatically
        self._dirty = False
        self._dir_mtimes: dict[Path, int] = {}  # folder -> st_mtime_ns at the last rescan
        self._peaks: dict[str, list[float]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.config.recording.output_dir)

    def start(self) -> None:
        try:
            organize(self.root)  # legacy loose files -> date folders (never touches open files: those are in date folders)
        except OSError:
            logger.exception("organize() failed")
        self.rescan()
        self._thread = threading.Thread(target=self._maintain, daemon=True, name="library")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.save()

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            mirrored = sorted(self._mirrored)
        # Outside the lock: serializing the caches takes a while, and add_file()
        # (called from the scan engine) waits for the lock.
        self.transcripts.save()
        self.quality.save()
        atomic_write_text(MIRRORED_FILE, json.dumps(mirrored))

    def _load_mirrored(self) -> set[str]:
        try:
            return set(json.loads(MIRRORED_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return set()

    def _maintain(self) -> None:
        last_rescan = last_retention = time.monotonic()
        while not self._stop.wait(_SAVE_INTERVAL_S):
            self.save()
            self._analyze_some(budget_s=2.0)
            now = time.monotonic()
            if now - last_rescan >= _RESCAN_INTERVAL_S:
                last_rescan = now
                self.rescan()
            if now - last_retention >= _RETENTION_INTERVAL_S:
                last_retention = now
                self.apply_retention()

    # -- indexing ---------------------------------------------------------
    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def _make(self, path: Path) -> Optional[Recording]:
        parsed = parse_filename(path.name)
        if parsed is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        rec = Recording(
            path=path, dt_local=parsed.dt_local, dt_utc=parsed.dt_local.astimezone(UTC_TZ),
            freq_khz=round(snap_airband_hz(parsed.freq_khz * 1000) / 1000), size=st.st_size,
        )
        rec.key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
        rec.quality = self.quality.get(rec.key)
        rec.transcript = self.transcripts.get(rec.key)
        if rec.transcript is not None and rec.transcript.status == "error":
            # Errors (a model still downloading, a broken install) are only
            # held back for the session that hit them (_failed) - retry after a restart.
            rec.transcript = None
        rec.is_favorite = self.favorites.is_favorite(rec.key)
        return rec

    def rescan(self) -> None:
        root = self.root
        if not root.is_dir():
            with self._lock:
                self._recs, self._blobs = {}, {}
            return
        open_now = {p.resolve() for p in self._open_paths()}
        open_names = {p.name for p in open_now}
        found: dict[str, Recording] = {}
        with self._lock:
            known = dict(self._recs)
        known_by_dir: dict[str, list[str]] = {}
        for rel in known:
            known_by_dir.setdefault(rel.rpartition("/")[0], []).append(rel)
        # Only folders whose listing changed (a file added, removed or renamed changes the folder's
        # mtime) are read file by file - usually just today's. A full pass over 46k files every
        # 2 minutes took ~3 s of CPU and starved the scan loop: a series of gaps in live audio.
        mtimes: dict[Path, int] = {}
        # At any depth, like the old glob("**/*.wav"): recordings a user filed under archive/2026-10/ stay listed.
        folders = [Path(dirpath) for dirpath, _dirs, _files in os.walk(root)]
        folders.sort()
        for folder in folders:
            try:
                mtime = folder.stat().st_mtime_ns
            except OSError:
                continue
            prefix = "" if folder == root else folder.relative_to(root).as_posix()
            if self._dir_mtimes.get(folder) == mtime:
                found.update((rel, known[rel]) for rel in known_by_dir.get(prefix, []))
                mtimes[folder] = mtime
                continue
            complete = True
            for path in sorted([*folder.glob("*.wav"), *folder.glob("*.mp3")]):
                if path.name in open_names and path.resolve() in open_now:
                    complete = False  # look again next time: it is listed once closed
                    continue
                rel = path.relative_to(root).as_posix()
                old = known.get(rel)
                try:
                    st = path.stat()
                except OSError:
                    continue
                if old is not None and old.key == f"{path}|{st.st_size}|{int(st.st_mtime)}":
                    found[rel] = old
                    continue
                rec = self._make(path)
                if rec is not None:
                    found[rel] = rec
            if complete:
                mtimes[folder] = mtime
        with self._lock:
            self._recs = found
            self._blobs = {rel: b for rel, b in self._blobs.items() if rel in found}
        self._dir_mtimes = mtimes

    def add_file(self, path: Path) -> None:
        """A recording just closed (engine) or arrived (upload)."""
        rec = self._make(Path(path))
        if rec is None:
            return
        with self._lock:
            rel = self._rel(rec.path)
            self._recs[rel] = rec
            self._blobs.pop(rel, None)

    def _analyze_some(self, budget_s: float) -> None:
        deadline = time.monotonic() + budget_s
        with self._lock:
            todo = [r for r in self._recs.values() if r.quality is None]
        todo.sort(key=lambda r: r.dt_utc, reverse=True)
        for rec in todo:
            if time.monotonic() > deadline or self._stop.is_set():
                return
            quality = analyze_wav(rec.path)
            with self._lock:
                rec.quality = quality
                self.quality.put(rec.key, quality)
                self._dirty = True

    # -- reading ----------------------------------------------------------
    def get(self, rel: str) -> Optional[Recording]:
        with self._lock:
            return self._recs.get(rel)

    def path_of(self, rel: str) -> Path:
        """Validated absolute path of a listed recording, else FileNotFoundError."""
        rec = self.get(rel)
        if rec is None or not rec.path.is_file():
            raise FileNotFoundError(rel)
        return rec.path

    def _blob(self, rel: str, rec: Recording, names: dict[int, str]) -> str:
        blob = self._blobs.get(rel)
        if blob is None:
            blob = " ".join((
                f"{rec.freq_khz / 1000:.3f}", airband_channel(rec.freq_khz * 1000, ch833=True), names.get(rec.freq_khz, ""),
                rec.dt_local.strftime("%Y-%m-%d %H:%M:%S"), rec.dt_utc.strftime("%Y-%m-%d %H:%M"),
                rec.path.name, rec.transcript_text,
            )).lower()
            self._blobs[rel] = blob
        return blob

    def _select(self, sel: Iterable[str], q: str, since: Optional[float], until: Optional[float],
                min_quality: float) -> list[tuple[str, Recording]]:
        sel = [s for s in sel if s and s != "all"]
        bands = [tuple(int(x) for x in s[5:].split("-")) for s in sel if s.startswith("band:")]
        freqs = {int(s[5:]) for s in sel if s.startswith("freq:")}
        langs = {s[5:] for s in sel if s.startswith("lang:")}
        fav = "fav" in sel
        q = q.strip().lower()
        names = self._freq_names() if q else {}
        out = []
        with self._lock:
            for rel, rec in self._recs.items():
                if sel:
                    lang = rec.transcript.language if rec.transcript and rec.transcript.language else ""
                    if not ((fav and rec.is_favorite) or rec.freq_khz in freqs or lang in langs
                            or any(lo <= rec.freq_khz * 1000 <= hi for lo, hi in bands)):
                        continue
                ts = rec.dt_utc.timestamp()
                if since is not None and ts < since:
                    continue
                if until is not None and ts > until:
                    continue
                if min_quality > 0 and (rec.quality is None or rec.quality.quality_score < min_quality):
                    continue
                if q and q not in self._blob(rel, rec, names):
                    continue
                out.append((rel, rec))
        return out

    def item(self, rel: str, rec: Recording, names: dict[int, str]) -> dict:
        qm = rec.quality
        with self._lock:
            leased = rel in self._leases or rel in self._requests
        return {
            "id": rel,
            "freq_khz": rec.freq_khz,
            "name": names.get(rec.freq_khz, ""),
            "manual": rec.freq_khz == MANUAL_FREQ_KHZ,
            "time": rec.dt_utc.isoformat(),
            "size": rec.size,
            "duration": qm.duration_sec if qm else None,
            "quality": round(qm.quality_score, 1) if qm and qm.readable else None,
            "snr": round(qm.snr_db, 1) if qm and qm.readable else None,
            "clipping": round(qm.clipping_ratio, 4) if qm and qm.readable else None,
            "transcript": transcript_dict(rec.transcript),
            "queued": leased,
            "favorite": rec.is_favorite,
            "mirrored": rel in self._mirrored,
        }

    def query(self, sel=(), q: str = "", since=None, until=None, min_quality: float = 0.0,
              sort: str = "new", offset: int = 0, limit: int = 100) -> dict:
        rows = self._select(sel, q, since, until, min_quality)
        if sort == "quality":
            rows.sort(key=lambda r: (r[1].quality.quality_score if r[1].quality else -1), reverse=True)
        elif sort == "old":
            rows.sort(key=lambda r: r[1].dt_utc)
        elif sort == "long":
            rows.sort(key=lambda r: (r[1].quality.duration_sec if r[1].quality else 0), reverse=True)
        else:
            rows.sort(key=lambda r: r[1].dt_utc, reverse=True)
        names = self._freq_names()
        page = rows[offset:offset + limit]
        return {
            "total": len(rows),
            "total_bytes": sum(r.size for _, r in rows),
            "items": [self.item(rel, rec, names) for rel, rec in page],
        }

    def ids(self, sel=(), q: str = "", since=None, until=None, min_quality: float = 0.0) -> list[str]:
        return [rel for rel, _ in self._select(sel, q, since, until, min_quality)]

    def facets(self) -> dict:
        """Sidebar groups: frequencies, band presets and languages, each with
        a count and average quality (same idea as the old Playback sidebar)."""
        with self._lock:
            recs = list(self._recs.values())
        names = self._freq_names()

        def agg(group: list[Recording]) -> dict:
            q = [r.quality.quality_score for r in group if r.quality and r.quality.readable]
            return {
                "count": len(group),
                "quality": round(sum(q) / len(q), 1) if q else None,
                "last": max(r.dt_utc for r in group).isoformat(),
            }

        by_freq: dict[int, list[Recording]] = {}
        by_lang: dict[str, list[Recording]] = {}
        for r in recs:
            by_freq.setdefault(r.freq_khz, []).append(r)
            lang = r.transcript.language if r.transcript and r.transcript.language else ""
            by_lang.setdefault(lang, []).append(r)
        bands = []
        for preset in get_all_presets():
            members = [r for r in recs if preset.start_hz <= r.freq_khz * 1000 <= preset.stop_hz]
            if members:
                bands.append({"key": f"band:{preset.start_hz}-{preset.stop_hz}", "label": preset.label,
                              "start_hz": preset.start_hz, **agg(members)})
        return {
            "total": len(recs),
            "favorites": sum(1 for r in recs if r.is_favorite),
            "frequencies": [{"key": f"freq:{f}", "freq_khz": f, "name": names.get(f, ""), **agg(g)}
                            for f, g in by_freq.items()],
            "bands": bands,
            "languages": [{"key": f"lang:{lang}", "language": lang, **agg(g)} for lang, g in by_lang.items()],
        }

    def stats(self, sel=(), utc: bool = False) -> dict:
        recs = [rec for _, rec in self._select(sel, "", None, None, 0.0)]
        summary = stats.summarize(recs, use_utc=utc)
        names = self._freq_names()
        return {
            "summary": {
                "count": summary.total_count,
                "duration": summary.total_duration_sec,
                "from": summary.date_from.isoformat() if summary.date_from else None,
                "to": summary.date_to.isoformat() if summary.date_to else None,
                "quality": round(summary.avg_quality, 1),
                "frequencies": summary.unique_frequencies,
                "busiest": summary.busiest_freq.freq_khz if summary.busiest_freq else None,
            },
            "frequencies": [
                {"freq_khz": s.freq_khz, "name": names.get(s.freq_khz, ""), "count": s.count,
                 "quality": round(s.avg_quality, 1), "duration": s.total_duration_sec,
                 "last": s.last_heard.isoformat()}
                for s in stats.frequency_stats(recs, use_utc=utc)
            ],
            "per_day": stats.counts_per_day(recs, use_utc=utc),
            "per_hour": stats.counts_per_hour(recs, use_utc=utc),
            "per_weekday": [c for _, c in stats.counts_per_weekday(recs, use_utc=utc)],
        }

    def peaks(self, rel: str) -> list[float]:
        cached = self._peaks.get(rel)
        if cached is None:
            values = compute_waveform_peaks(self.path_of(rel), 600)
            cached = [round(float(v), 3) for v in values] if values is not None else []
            if len(self._peaks) > 300:
                self._peaks.clear()
            self._peaks[rel] = cached
        return cached

    def storage(self) -> dict:
        import shutil
        with self._lock:
            recs = list(self._recs.values())
        try:
            disk = shutil.disk_usage(self.root if self.root.exists() else self.root.parent)
            disk_info = {"total": disk.total, "free": disk.free}
        except OSError:
            disk_info = {"total": None, "free": None}
        oldest = min((r.dt_utc for r in recs), default=None)
        return {
            "root": str(self.root),
            "count": len(recs),
            "bytes": sum(r.size for r in recs),
            "oldest": oldest.isoformat() if oldest else None,
            "mirrored": sum(1 for r in recs if self._rel_safe(r) in self._mirrored),
            "favorites": sum(1 for r in recs if r.is_favorite),
            **disk_info,
        }

    def _rel_safe(self, rec: Recording) -> str:
        try:
            return self._rel(rec.path)
        except ValueError:
            return ""

    # -- writing ----------------------------------------------------------
    def set_favorite(self, rels: Iterable[str], favorite: bool) -> None:
        with self._lock:
            for rel in rels:
                rec = self._recs.get(rel)
                if rec is not None:
                    rec.is_favorite = favorite
                    self.favorites.set_favorite(rec.key, favorite)

    def delete(self, rels: Iterable[str]) -> int:
        deleted = 0
        with self._lock:
            for rel in rels:
                rec = self._recs.pop(rel, None)
                if rec is None:
                    continue
                try:
                    rec.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    self._recs[rel] = rec
                    logger.exception("Could not delete %s", rec.path)
                    continue
                self.quality.remove(rec.key)
                self.transcripts.remove(rec.key)
                self.favorites.remove(rec.key)
                self._mirrored.discard(rel)
                self._blobs.pop(rel, None)
                self._requests.pop(rel, None)
                self._leases.pop(rel, None)
                deleted += 1
            if deleted:
                self.favorites.save()
                self._dirty = True
        return deleted

    def set_transcript_text(self, rel: str, text: str) -> None:
        with self._lock:
            rec = self._recs.get(rel)
            if rec is None:
                raise FileNotFoundError(rel)
            old = rec.transcript
            rec.transcript = TranscriptionResult(
                text=text.strip(), language=old.language if old else "", language_probability=1.0,
                status="ok", engine="manual", transcribed_at=datetime.now(timezone.utc).isoformat(),
            )
            self.transcripts.put(rec.key, rec.transcript)
            self._blobs.pop(rel, None)
            self._dirty = True

    def request_transcription(self, rels: Iterable[str], language: Optional[str] = None) -> None:
        with self._lock:
            for rel in rels:
                if rel in self._recs:
                    self._requests[rel] = language or None
                    self._failed.discard(rel)

    def mark_mirrored(self, rels: Iterable[str]) -> None:
        with self._lock:
            for rel in rels:
                if rel in self._recs:
                    self._mirrored.add(rel)
            self._dirty = True

    # -- worker protocol ----------------------------------------------------
    def pending_count(self) -> int:
        """Recordings still waiting for a transcript (what an auto-transcribing
        worker would work through), for the UI's progress line."""
        with self._lock:
            auto = self.config.transcription.enabled
            return len(self._requests) + (sum(
                1 for rel, r in self._recs.items()
                if r.transcript is None and rel not in self._failed and rel not in self._requests
            ) if auto else 0)

    def claim_job(self, worker: str) -> Optional[dict]:
        now = time.monotonic()
        with self._lock:
            self._leases = {rel: v for rel, v in self._leases.items() if v[1] > now}
            rel = next((r for r in self._requests if r not in self._leases and r in self._recs), None)
            if rel is None and self.config.transcription.enabled:
                candidates = [
                    (r.dt_utc, rel) for rel, r in self._recs.items()
                    if r.transcript is None and rel not in self._leases and rel not in self._failed
                ]
                rel = max(candidates)[1] if candidates else None
            if rel is None:
                return None
            rec = self._recs[rel]
            self._leases[rel] = (worker, now + _LEASE_S)
            return {
                "id": rel, "key": rec.key, "size": rec.size,
                "duration": rec.quality.duration_sec if rec.quality else 0.0,
                "language": self._requests.get(rel), "path": str(rec.path),
            }

    def submit_result(self, rel: str, key: str, result: dict) -> bool:
        with self._lock:
            self._leases.pop(rel, None)
            self._requests.pop(rel, None)
            rec = self._recs.get(rel)
            if rec is None or rec.key != key:
                return False  # deleted or rewritten meanwhile
            rec.transcript = _from_fields(TranscriptionResult, result)
            if rec.transcript.status == "error":
                self._failed.add(rel)
            self.transcripts.put(rec.key, rec.transcript)
            self._blobs.pop(rel, None)
            self._dirty = True
            return True

    def release_job(self, rel: str) -> None:
        with self._lock:
            self._leases.pop(rel, None)

    def files(self) -> list[dict]:
        """Every listed recording with its size - what a mirroring worker diffs against."""
        with self._lock:
            return [{"id": rel, "size": r.size, "mtime": int(r.key.rsplit("|", 1)[1])}
                    for rel, r in self._recs.items()]

    def import_metadata(self, items: list[dict]) -> int:
        """A client's own transcripts/quality/favorites for files the server
        also has (matched by relative path + size) - only fills in what the
        server is missing, never overwrites."""
        n = 0
        with self._lock:
            for item in items:
                rec = self._recs.get(str(item.get("id", "")))
                if rec is None or rec.size != item.get("size"):
                    continue
                if item.get("transcript") and rec.transcript is None:
                    rec.transcript = _from_fields(TranscriptionResult, item["transcript"])
                    self.transcripts.put(rec.key, rec.transcript)
                    self._blobs.pop(str(item["id"]), None)
                    n += 1
                if item.get("quality") and rec.quality is None:
                    rec.quality = _from_fields(QualityMetrics, item["quality"])
                    self.quality.put(rec.key, rec.quality)
                if item.get("favorite") and not rec.is_favorite:
                    rec.is_favorite = True
                    self.favorites.set_favorite(rec.key, True)
            self._dirty = True
        return n

    def upload_target(self, rel: str) -> Path:
        """Where an uploaded file with this relative path goes - validated to
        stay inside the output folder and to be a recognized recording name."""
        root = self.root.resolve()
        target = (root / rel).resolve()
        if root not in target.parents or parse_filename(target.name) is None:
            raise ValueError("Invalid recording path")
        return target

    # -- retention ----------------------------------------------------------
    def retention_victims(self) -> list[str]:
        cfg = self.config.recording
        if cfg.retention_days <= 0 and cfg.retention_max_gb <= 0:
            return []
        with self._lock:
            recs = sorted(self._recs.items(), key=lambda kv: kv[1].dt_utc)
            eligible = [
                (rel, r) for rel, r in recs
                if not (cfg.retention_keep_favorites and r.is_favorite)
                and not (cfg.retention_require_mirrored and rel not in self._mirrored)
            ]
        victims: list[str] = []
        if cfg.retention_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.retention_days)
            victims = [rel for rel, r in eligible if r.dt_utc < cutoff]
        if cfg.retention_max_gb > 0:
            limit = cfg.retention_max_gb * 1e9
            chosen = set(victims)
            remaining = sum(r.size for _, r in recs) - sum(r.size for rel, r in eligible if rel in chosen)
            for rel, r in eligible:
                if remaining <= limit:
                    break
                if rel not in chosen:
                    victims.append(rel)
                    remaining -= r.size
        return victims

    def apply_retention(self) -> int:
        victims = self.retention_victims()
        n = self.delete(victims) if victims else 0
        if n:
            logger.info("Retention removed %d recordings", n)
        return n
