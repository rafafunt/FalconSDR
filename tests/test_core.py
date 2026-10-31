"""Run: python -m unittest tests/test_core.py

Covers the branches that touch data or access: config load/save, which
recording files the library exposes (and to whom), retention, the worker
job/copy protocol, and the HTTP layer's password/CSRF checks.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Every path the app derives from ~ (config, caches) must land in a scratch
# home - set before importing falconsdr.
_HOME = tempfile.mkdtemp(prefix="falconsdr-test-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME  # Path.home() on Windows

from falconsdr import config as config_mod  # noqa: E402
from falconsdr.config import AppConfig  # noqa: E402
from falconsdr.playback.index import Library  # noqa: E402

NAME = "2026-01-01_10-00-00_{khz}.wav"


def _wav(root: Path, rel: str, size: int = 100) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


class _LibCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir=_HOME))
        self.root = self.tmp / "rec"
        self.root.mkdir()
        self.cfg = AppConfig()
        self.cfg.recording.output_dir = str(self.root)
        self.open: set[Path] = set()
        self.lib = Library(self.cfg, lambda: self.open, lambda: {})


class ConfigTest(unittest.TestCase):
    def test_unknown_keys_are_ignored_and_save_is_atomic(self) -> None:
        config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config_mod.CONFIG_PATH.write_text(json.dumps({
            "device": {"driver": "rtlsdr", "from_a_newer_version": 1},
            "recording": {"output_dir": "/x", "mode": "split"},
            "bands": [{"start_hz": 1, "stop_hz": 2, "future_field": True}],
        }))
        cfg = AppConfig.load()
        self.assertEqual((cfg.device.driver, cfg.recording.output_dir, cfg.bands[0].stop_hz), ("rtlsdr", "/x", 2))
        cfg.trigger_level_dbfs = -42.0
        cfg.save()
        self.assertEqual(AppConfig.load().trigger_level_dbfs, -42.0)
        self.assertFalse(list(config_mod.CONFIG_DIR.glob("*.tmp")))

    def test_a_corrupt_config_is_kept_aside_not_overwritten(self) -> None:
        config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config_mod.CONFIG_PATH.write_text('{"access_token": "secret", ')
        cfg = AppConfig.load()
        self.assertEqual(cfg.access_token, "")  # defaults for now ...
        cfg.save()
        bad = config_mod.CONFIG_PATH.with_name("config.json.bad")
        self.assertIn("secret", bad.read_text())  # ... but the original is still there
        bad.unlink()

    def test_concurrent_saves_do_not_corrupt_the_file(self) -> None:
        target = Path(tempfile.mkdtemp(dir=_HOME)) / "x.json"
        errors = []
        def hammer(n: int) -> None:
            try:
                for _ in range(200):
                    config_mod.atomic_write_text(target, json.dumps({"v": "x" * (n * 1000)}))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=hammer, args=(n,)) for n in (1, 5, 9)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        json.loads(target.read_text())
        self.assertFalse(list(target.parent.glob("*.tmp")))

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_the_config_holding_the_password_is_private(self) -> None:
        cfg = AppConfig()
        cfg.access_token = "secret"
        cfg.save()
        self.assertEqual(config_mod.CONFIG_PATH.stat().st_mode & 0o077, 0)

    def test_empty_recordings_folder_means_default(self) -> None:
        config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config_mod.CONFIG_PATH.write_text(json.dumps({"recording": {"output_dir": "  "}}))
        self.assertEqual(AppConfig.load().recording.output_dir, config_mod.DEFAULT_RECORDINGS_DIR)

    def test_non_ascii_files_load_on_any_locale(self) -> None:
        # Windows' default text encoding is cp1250/cp1252 - every reader must say utf-8.
        from falconsdr.playback.transcription_cache import TranscriptionCache
        cache = Path(tempfile.mkdtemp(dir=_HOME)) / "t.json"
        cache.write_text(json.dumps({"k": {"text": "Wieża Okęcie, dzień dobry ‘’"}}, ensure_ascii=False), encoding="utf-8")
        self.assertIn("Okęcie", TranscriptionCache(cache)._data["k"]["text"])
        config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config_mod.CONFIG_PATH.write_text(json.dumps({"bands": [{"label": "Łączność ‘FM’"}]}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(AppConfig.load().bands[0].label, "Łączność ‘FM’")

    def test_transcription_languages_follow_the_system(self) -> None:
        from unittest import mock
        from falconsdr.config import TranscriptionConfig
        for env, want in (("de_DE.UTF-8", ["en", "de"]), ("en_GB.UTF-8", ["en"]), ("Polish_Poland", None)):
            with mock.patch.dict(os.environ, {"LC_ALL": env}):
                langs = TranscriptionConfig().candidate_languages
                if want:
                    self.assertEqual(langs, want)
                else:  # a Windows-style name is never read as "po"
                    self.assertNotIn("po", langs)

    def test_list_dirs(self) -> None:
        from falconsdr.web.server import list_dirs
        root = Path(tempfile.mkdtemp(dir=_HOME))
        (root / "b").mkdir()
        (root / "A").mkdir()
        (root / ".hidden").mkdir()
        (root / "file.txt").write_text("x")
        out = list_dirs(str(root / "not" / "yet"))  # opens at the nearest existing parent
        self.assertEqual(Path(out["path"]), root.resolve())
        self.assertEqual([d["name"] for d in out["dirs"]], ["A", "b"])
        self.assertEqual(Path(out["dirs"][0]["path"]), root.resolve() / "A")


class AutostartTest(unittest.TestCase):
    def test_login_item_never_opens_the_browser_and_quotes_paths(self) -> None:
        from falconsdr import autostart
        self.assertEqual(autostart.command()[-1], "--no-browser")
        self.assertEqual(autostart._desktop_exec(["/opt/falcon sdr/x", "--no-browser"]), '"/opt/falcon sdr/x" --no-browser')
        self.assertEqual(autostart._desktop_exec(['/a"b$c']), r'"/a\"b\$c"')


class LibraryTest(_LibCase):
    def test_rescan_reads_only_folders_whose_listing_changed(self) -> None:
        import os
        from unittest import mock
        a = _wav(self.root, "2026-10-01/2026-10-01_12-00-00_130875.wav")
        _wav(self.root, "2026-10-02/2026-10-02_12-00-00_124625.wav")
        self.lib.rescan()
        self.assertEqual(len(self.lib._recs), 2)
        stat_calls = []
        real_stat = Path.stat
        with mock.patch.object(Path, "stat", lambda p, **kw: (stat_calls.append(p), real_stat(p, **kw))[1]):
            self.lib.rescan()  # nothing changed: folders are stat'ed, not their files
        self.assertEqual(len(self.lib._recs), 2)
        self.assertNotIn(a, stat_calls)
        b = _wav(self.root, "2026-10-02/2026-10-02_13-00-00_124625.wav")  # new file
        a.unlink()  # removed file
        for folder in (a.parent, b.parent):  # same-second changes: make the mtime differ for sure
            st = folder.stat()
            os.utime(folder, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        self.lib.rescan()
        self.assertEqual(sorted(self.lib._recs), ["2026-10-02/2026-10-02_12-00-00_124625.wav",
                                                  "2026-10-02/2026-10-02_13-00-00_124625.wav"])

    def test_folders_at_any_depth_are_listed_and_follow_changes(self) -> None:
        import os
        nested = _wav(self.root, "archive/2026-10/2026-10-03_12-00-00_130875.wav")
        _wav(self.root, "2026-10-04/2026-10-04_12-00-00_124625.wav")
        self.lib.rescan()
        self.assertEqual(sorted(self.lib._recs), ["2026-10-04/2026-10-04_12-00-00_124625.wav",
                                                  "archive/2026-10/2026-10-03_12-00-00_130875.wav"])
        self.lib.rescan()  # unchanged: the nested recording is kept
        self.assertEqual(len(self.lib._recs), 2)
        _wav(self.root, "archive/2026-10/2026-10-03_13-00-00_130875.wav")
        st = nested.parent.stat()
        os.utime(nested.parent, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        self.lib.rescan()
        self.assertEqual(len(self.lib._recs), 3)

    def test_a_folder_with_an_open_recording_is_read_again_next_time(self) -> None:
        c = _wav(self.root, "2026-10-03/2026-10-03_12-00-00_130875.wav")
        self.open = {c}
        self.lib.rescan()
        self.assertEqual(self.lib._recs, {})
        self.open = set()  # closed - but the folder's listing did not change since
        self.lib.rescan()
        self.assertEqual(list(self.lib._recs), ["2026-10-03/2026-10-03_12-00-00_130875.wav"])

    def test_open_files_and_paths_outside_the_library_are_never_exposed(self) -> None:
        _wav(self.root, "2026-01-01/" + NAME.format(khz=121500))
        writing = _wav(self.root, "2026-01-01/" + NAME.format(khz=118000))
        _wav(self.tmp, NAME.format(khz=1))  # outside the root
        self.open = {writing}
        self.lib.rescan()
        self.assertEqual([f["id"] for f in self.lib.files()], ["2026-01-01/" + NAME.format(khz=121500)])
        for bad in ("../" + NAME.format(khz=1), "2026-01-01/" + NAME.format(khz=118000)):
            with self.assertRaises(FileNotFoundError):
                self.lib.path_of(bad)
        for bad in ("../" + NAME.format(khz=1), "2026-01-01/notes.txt", "/etc/x.wav"):
            with self.assertRaises(ValueError):
                self.lib.upload_target(bad)
        self.assertEqual(self.lib.upload_target("a/" + NAME.format(khz=5)), (self.root / "a" / NAME.format(khz=5)).resolve())

    def test_retention_keeps_favorites_and_unmirrored_copies(self) -> None:
        old = ["2020-01-0%d/2020-01-0%d_10-00-00_12150%d.wav" % (i, i, i) for i in range(1, 4)]
        for rel in old:
            _wav(self.root, rel)
        self.lib.rescan()
        self.cfg.recording.retention_days = 30
        self.lib.set_favorite([old[0]], True)
        self.lib.mark_mirrored([old[1]])
        self.cfg.recording.retention_require_mirrored = True
        self.assertEqual(self.lib.retention_victims(), [old[1]])
        self.cfg.recording.retention_require_mirrored = False
        self.assertEqual(sorted(self.lib.retention_victims()), [old[1], old[2]])
        self.assertEqual(self.lib.apply_retention(), 2)
        self.assertTrue((self.root / old[0]).exists())

    def test_jobs_are_leased_and_stale_results_rejected(self) -> None:
        rel = "2026-01-01/" + NAME.format(khz=121500)
        _wav(self.root, rel)
        self.lib.rescan()
        job = self.lib.claim_job("pc")
        self.assertEqual(job["id"], rel)
        self.assertIsNone(self.lib.claim_job("server"))  # leased to "pc"
        result = {"text": "hi", "language": "en", "language_probability": 1.0, "status": "ok",
                  "engine": "x", "transcribed_at": "now"}
        self.assertFalse(self.lib.submit_result(rel, "stale-key", result))
        self.assertTrue(self.lib.submit_result(rel, job["key"], result))
        self.assertEqual(self.lib.get(rel).transcript.text, "hi")

    def test_failed_transcription_waits_for_the_next_session_only(self) -> None:
        rel = "2026-01-01/" + NAME.format(khz=121500)
        _wav(self.root, rel)
        self.lib.rescan()
        job = self.lib.claim_job("pc")
        error = {"text": "", "language": "", "language_probability": 0.0, "status": "error",
                 "engine": "", "transcribed_at": "now"}
        self.assertTrue(self.lib.submit_result(rel, job["key"], error))
        self.assertIsNone(self.lib.claim_job("pc"))  # not retried in this session
        fresh = Library(self.cfg, lambda: self.open, lambda: {})  # program restarted
        fresh.rescan()
        self.assertEqual(fresh.claim_job("pc")["id"], rel)

    def test_packaged_uninstall_waits_for_a_restart_only_while_in_use(self) -> None:
        import sys
        from unittest import mock
        from falconsdr.playback import transcription_install as ti
        lines: list[str] = []
        with mock.patch.object(sys, "frozen", True, create=True):
            ti.enable_addons()
            (ti.ADDONS_DIR / "faster_whisper").mkdir(exist_ok=True)
            with mock.patch.object(ti, "_loaded", return_value=True):  # Windows: files locked
                self.assertEqual(ti.uninstall_transcription([], lines.append), (True, True, ""))
            self.assertTrue((ti.ADDONS_DIR / "faster_whisper").exists())
            ti.enable_addons()  # next start
            self.assertFalse((ti.ADDONS_DIR / "faster_whisper").exists())
            self.assertFalse(ti._REMOVE_MARKER.exists())
            (ti.ADDONS_DIR / "faster_whisper").mkdir()
            with mock.patch.object(ti, "_loaded", return_value=False):
                self.assertEqual(ti.uninstall_transcription([], lines.append), (True, False, ""))
            self.assertFalse((ti.ADDONS_DIR / "faster_whisper").exists())
            self.assertTrue(ti.ADDONS_DIR.is_dir())  # still there for a later install
            # Another process still holds a .pyd: rmtree leaves part of it - finish on restart.
            (ti.ADDONS_DIR / "faster_whisper").mkdir()
            with mock.patch.object(ti, "_loaded", return_value=False), \
                    mock.patch.object(ti.shutil, "rmtree"), mock.patch.object(ti.time, "sleep"):
                self.assertEqual(ti.uninstall_transcription([], lines.append), (True, True, ""))
                self.assertTrue(ti.removal_pending())
                sys.path.remove(str(ti.ADDONS_DIR))
                ti.enable_addons()  # restart while still locked: marker stays, broken files stay off sys.path
                self.assertNotIn(str(ti.ADDONS_DIR), sys.path)
            self.assertTrue(ti.removal_pending())
            ok, _, _ = ti.install_transcription(False, lines.append)  # never over half-removed files
            self.assertFalse(ok)
            ti.enable_addons()
            self.assertFalse((ti.ADDONS_DIR / "faster_whisper").exists())
            self.assertFalse(ti._REMOVE_MARKER.exists())

    def test_uninstall_deletes_only_the_chosen_models(self) -> None:
        from unittest import mock
        from falconsdr.playback import transcription_install as ti
        cache = Path(tempfile.mkdtemp())
        for repo in ("Systran--faster-whisper-large-v3", "jacktol--atc", "someone--other"):
            (cache / f"models--{repo}" / "blobs").mkdir(parents=True)
            (cache / f"models--{repo}" / "blobs" / "x").write_bytes(b"0" * 10)
        lines: list[str] = []
        with mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(cache)}):
            ti.delete_models(["large-v3", "jacktol/atc", "medium.en"], lines.append)
        self.assertEqual(sorted(p.name for p in cache.iterdir()), ["models--someone--other"])
        self.assertTrue(any("not downloaded" in line for line in lines))

    def test_model_load_failure_keeps_the_recording_queued(self) -> None:
        import sys
        import types
        from unittest import mock
        from falconsdr.playback import transcription_engine as te
        from falconsdr.playback.models import Recording
        fake = types.ModuleType("faster_whisper.audio")
        fake.decode_audio = lambda path: None
        rec = Recording(path=self.root / "x.wav", dt_local=None, dt_utc=None, freq_khz=0)
        with mock.patch.dict(sys.modules, {"faster_whisper": types.ModuleType("faster_whisper"), "faster_whisper.audio": fake}),                 mock.patch.object(te, "_get_models", side_effect=RuntimeError("Unable to open file 'model.bin'")):
            with self.assertRaises(te.ModelUnavailable):
                te.transcribe_recording(rec, AppConfig().transcription)

    def test_import_matches_by_size_and_never_overwrites(self) -> None:
        rel = "2026-01-01/" + NAME.format(khz=121500)
        _wav(self.root, rel, size=100)
        self.lib.rescan()
        tr = {"text": "a", "language": "pl", "language_probability": 1, "status": "ok", "engine": "x", "transcribed_at": "t"}
        self.assertEqual(self.lib.import_metadata([{"id": rel, "size": 99, "transcript": tr}]), 0)
        self.assertEqual(self.lib.import_metadata([{"id": rel, "size": 100, "transcript": tr, "favorite": True}]), 1)
        self.assertEqual(self.lib.import_metadata([{"id": rel, "size": 100, "transcript": {**tr, "text": "b"}}]), 0)
        rec = self.lib.get(rel)
        self.assertEqual((rec.transcript.text, rec.is_favorite), ("a", True))


class GridTest(unittest.TestCase):
    def test_airband_raster_is_exact_and_windows_stay_on_it(self) -> None:
        from falconsdr.config import VfoRangeConfig
        from falconsdr.scanner_engine import _band_grid, _wide_anchor_windows
        band = VfoRangeConfig(start_hz=121_000_000, stop_hz=136_800_000, step_hz=8333)
        grid = set(_band_grid(band)[0])
        for carrier in (131_400_000, 126_800_000, 126_808_333, 126_816_667):
            self.assertIn(carrier, grid)  # integer 8333 steps drifted to 131.399584
        for window in _wide_anchor_windows([131_400_000], band, 2.4e6):
            self.assertTrue(set(window.freqs) <= grid)


class AirbandChannelTest(unittest.TestCase):
    def test_carrier_to_published_channel_name(self) -> None:
        from falconsdr.models import airband_channel
        self.assertEqual(airband_channel(126_800_000, ch833=True), "126.805")
        self.assertEqual(airband_channel(130_875_000), "130.875")  # 25 kHz-grid carrier: 25 kHz name by default
        self.assertEqual(airband_channel(126_808_333), "126.810")
        self.assertEqual(airband_channel(126_817_000), "126.815")  # whole-kHz filename carrier (round(126816.667))
        self.assertEqual(airband_channel(121_500_000), "")         # guard stays 25 kHz
        self.assertEqual(airband_channel(148_750_000), "")         # not airband

    def test_near_duplicate_airband_frequencies_snap_to_one_channel(self) -> None:
        from falconsdr.models import snap_airband_hz
        self.assertEqual(snap_airband_hz(130_874_000), 130_875_000)
        self.assertEqual(snap_airband_hz(135_966_000), 135_966_667)
        self.assertEqual(snap_airband_hz(121_501_000), 121_501_000)  # guard untouched
        self.assertEqual(snap_airband_hz(149_162_000), 149_162_000)  # not airband

    def test_ch833_round_trips_through_csv_row(self) -> None:
        from falconsdr.models import Channel
        self.assertTrue(Channel.from_row(Channel(126_800_000, ch833=True).to_row()).ch833)
        self.assertFalse(Channel.from_row(["130875000", "x", "AM", "1", "", "0.0"]).ch833)  # old 6-column file


class SquelchTest(unittest.TestCase):
    def test_band_squelch_overrides_global_and_never_exceeds_trigger(self) -> None:
        from types import SimpleNamespace
        from falconsdr.config import VfoRangeConfig
        from falconsdr.scanner_engine import ScannerEngine
        cfg = AppConfig()
        cfg.squelch_enabled, cfg.squelch_offset_db = False, -6.0
        band = VfoRangeConfig(squelch_enabled=True, squelch_offset_db=4.0)  # positive offset must clamp to 0
        fake = SimpleNamespace(config=cfg, _effective_trigger_dbfs=lambda w: -48.0)
        win = SimpleNamespace(band=band)
        self.assertEqual(ScannerEngine._effective_squelch_dbfs(fake, win), -48.0)
        band.squelch_offset_db = -10.0
        self.assertEqual(ScannerEngine._effective_squelch_dbfs(fake, win), -58.0)
        self.assertIsNone(ScannerEngine._effective_squelch_dbfs(fake, SimpleNamespace(band=VfoRangeConfig())))
        cfg.squelch_enabled = True  # band without its own value follows the global one
        self.assertEqual(ScannerEngine._effective_squelch_dbfs(fake, SimpleNamespace(band=VfoRangeConfig())), -54.0)


class RawReadTest(unittest.TestCase):
    """RtlSdrBackend hands the dongle's bytes over unconverted: converting in the read thread left the
    dongle without an outstanding transfer for ~0.4 ms per block (1.5 % of the samples lost)."""

    def _backend(self, stream):
        import numpy as np
        from types import SimpleNamespace
        from falconsdr import sdr_backend as sb
        backend = object.__new__(sb.RtlSdrBackend)
        backend._carry = np.empty(0, dtype=np.uint8)
        backend._io_diag = SimpleNamespace(record_read=lambda *a: None)
        backend._async, backend._sync_reads, backend._stream_thread = False, 0, None
        pos = [0]

        def read_bytes(n):
            out = stream[pos[0]:pos[0] + n]
            pos[0] += n
            return out
        backend.device = SimpleNamespace(read_bytes=read_bytes)
        return backend

    def test_read_equals_convert_of_read_raw_and_carry_keeps_every_byte(self) -> None:
        import numpy as np
        rng = np.random.default_rng(0)
        stream = rng.integers(0, 256, size=4 * 1000 * 2 + 4096, dtype=np.uint8)
        a, b = self._backend(stream), self._backend(stream)
        for _ in range(4):  # 1000 samples = 2000 bytes: not a multiple of the 512-byte read granule
            raw = a.read_raw(1000)
            self.assertEqual((raw.dtype, raw.size), (np.uint8, 2000))
            np.testing.assert_array_equal(a.to_iq(raw), b.read(1000))
        np.testing.assert_array_equal(  # no byte lost or repeated across blocks
            np.concatenate([self._backend(stream).read_raw(1000) for _ in range(1)]), stream[:2000])

    def test_acquisition_thread_converts_on_the_consumer_side(self) -> None:
        import numpy as np
        from falconsdr.sdr_backend import AcquisitionThread
        calls = {"raw": 0, "iq": 0}

        class Fake:
            sample_rate, label, gain_range_db = 2_400_000.0, "fake", (0.0, 50.0)

            def read_raw(self, n):
                calls["raw"] += 1
                time.sleep(0.002)
                return np.full(2 * n, 200, dtype=np.uint8)

            def to_iq(self, raw):
                calls["iq"] += 1
                return raw.astype(np.float32).view(np.complex64)

            def read(self, n):
                raise AssertionError("the read loop must not use the converting read()")

            def close(self):
                pass

        thread = AcquisitionThread(Fake(), 100)
        try:
            iq = thread.read(100)
            self.assertEqual((iq.dtype, iq.size), (np.complex64, 100))
            time.sleep(0.05)
            self.assertGreater(calls["raw"], calls["iq"])  # read ahead of what was consumed, converted lazily
        finally:
            thread.close()


class AsyncStreamTest(unittest.TestCase):
    """RtlSdrBackend keeps USB transfers queued once the scanner stays on a frequency."""

    def _backend(self, lib, async_read=True):
        import sys
        import threading
        from collections import deque
        from types import ModuleType, SimpleNamespace
        import numpy as np
        from falconsdr import sdr_backend as sb
        backend = object.__new__(sb.RtlSdrBackend)
        backend._carry = np.empty(0, dtype=np.uint8)
        backend._io_diag = SimpleNamespace(record_read=lambda *a: None)
        backend._async, backend._sync_reads, backend._stream_thread = async_read, 0, None
        backend._stream_cb, backend._stream_cond, backend._stream_chunks = None, threading.Condition(), deque()
        backend._stream_error, backend._stream_stopping = None, False
        backend.sample_rate, backend.bias_tee = 2_400_000.0, False
        backend._stream_stats = sb._StreamStats(2_400_000.0)
        backend.device = SimpleNamespace(dev_p=1, read_bytes=lambda n: np.full(n, 5, dtype=np.uint8),
                                         set_bias_tee=lambda on: None, set_gain=lambda g: None)
        fake = ModuleType("rtlsdr.librtlsdr")
        fake.librtlsdr = lib
        import ctypes
        fake.rtlsdr_read_async_cb_t = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int, ctypes.py_object)
        return backend, {"rtlsdr": ModuleType("rtlsdr"), "rtlsdr.librtlsdr": fake}

    def _lib(self, result=0, fail_after_data=False, ignore_cancel=False):
        import ctypes
        import threading

        class Lib:
            def __init__(self):
                self.cancelled, self.release = threading.Event(), threading.Event()
                self.started, self.resets, self.lengths = 0, 0, []

            def rtlsdr_reset_buffer(self, dev):
                self.resets += 1

            def rtlsdr_read_async(self, dev, cb, ctx, num, length):
                self.started += 1
                self.lengths.append(length)
                if result < 0:
                    return result
                buf = (ctypes.c_ubyte * length).from_buffer_copy(bytes([7]) * length)
                if fail_after_data:
                    cb(ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), length, ctx)
                    return -5
                stop = self.release if ignore_cancel else self.cancelled
                while not stop.is_set():
                    cb(ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), length, ctx)
                    time.sleep(0.002)
                self.cancelled.clear()
                return 0

            def rtlsdr_cancel_async(self, dev):
                self.cancelled.set()
                return 0
        return Lib()

    def test_stream_starts_after_a_few_reads_and_stops_at_a_retune(self) -> None:
        import sys
        from unittest import mock
        lib = self._lib()
        backend, modules = self._backend(lib)
        with mock.patch.dict(sys.modules, modules):
            for _ in range(8):
                raw = backend.read_raw(1000)
                self.assertEqual((raw.size, int(raw[0])), (2000, 5))   # blocking reads while hopping
            self.assertEqual(lib.started, 0)
            raw = backend.read_raw(1000)                                # the 9th: the stream takes over
            self.assertEqual((raw.size, int(raw[0]), int(raw[-1])), (2000, 5, 7))  # carry of the blocking reads, then the stream
            self.assertEqual((lib.started, lib.resets), (1, 1))
            self.assertEqual(backend.read_raw(1000).size, 2000)
            backend.set_frequency(131_900_000)                          # retune: stream off, old data gone
            self.assertIsNone(backend._stream_thread)
            self.assertEqual(len(backend._stream_chunks), 0)
            self.assertEqual(int(backend.read_raw(1000)[0]), 5)         # hopping again: blocking reads

    def test_stream_off_when_disabled(self) -> None:
        import sys
        from unittest import mock
        lib = self._lib()
        backend, modules = self._backend(lib, async_read=False)
        with mock.patch.dict(sys.modules, modules):
            for _ in range(12):
                backend.read_raw(1000)
            self.assertEqual(lib.started, 0)

    def test_a_stream_that_cannot_start_falls_back_to_blocking_reads(self) -> None:
        import sys
        from unittest import mock
        lib = self._lib(result=-5)
        backend, modules = self._backend(lib)
        with mock.patch.dict(sys.modules, modules):
            for _ in range(8):
                backend.read_raw(1000)
            raw = backend.read_raw(1000)   # the stream fails at once: this read must still succeed
            self.assertEqual((raw.size, int(raw[-1])), (2000, 5))
            for _ in range(20):
                self.assertEqual(int(backend.read_raw(1000)[-1]), 5)
            self.assertEqual((lib.started, backend._async), (1, False))   # and it is not tried again

    def test_a_stream_that_dies_after_delivering_is_an_error(self) -> None:
        import sys
        from unittest import mock
        lib = self._lib(fail_after_data=True)   # one buffer, then the device is gone
        backend, modules = self._backend(lib)
        with mock.patch.dict(sys.modules, modules):
            for _ in range(8):
                backend.read_raw(1000)
            backend.read_raw(1000)
            with self.assertRaises(RuntimeError):
                for _ in range(200):
                    backend.read_raw(1000)

    def test_buffer_size_follows_the_sample_rate(self) -> None:
        import sys
        from unittest import mock
        for rate, expected in ((2_400_000.0, 65536), (250_000.0, 6656)):
            lib = self._lib()
            backend, modules = self._backend(lib)
            backend.sample_rate = rate
            with mock.patch.dict(sys.modules, modules):
                for _ in range(9):
                    backend.read_raw(1000)
                backend._stop_stream()
            self.assertEqual(lib.lengths, [expected])
            self.assertEqual(expected % 512, 0)

    def test_close_does_not_free_the_device_under_a_stuck_stream(self) -> None:
        import sys
        from types import SimpleNamespace
        from unittest import mock
        from falconsdr import sdr_backend as sb
        lib = self._lib(ignore_cancel=True)
        backend, modules = self._backend(lib)
        closed = []
        backend.device.close = lambda: closed.append(True)
        with mock.patch.dict(sys.modules, modules), mock.patch.object(sb, "_STREAM_STOP_TIMEOUT_S", 0.2):
            for _ in range(9):
                backend.read_raw(1000)
            backend.close()
            self.assertEqual(closed, [])   # the stream thread still runs: the handle stays
            lib.release.set()
            time.sleep(0.05)
            backend2, _ = self._backend(self._lib())
            backend2.device.close = lambda: closed.append(True)
            backend2.close()
            self.assertEqual(closed, [True])   # no stream: closed as usual

    def test_stats_tell_a_loss_from_a_late_callback(self) -> None:
        from falconsdr.sdr_backend import _StreamStats
        step = 65536 / 2 / 2_400_000.0   # one 64 KiB transfer
        ok, late, lossy = _StreamStats(2_400_000.0), _StreamStats(2_400_000.0), _StreamStats(2_400_000.0)
        t = 0.0
        for k in range(1000):            # ~13.6 s
            t += step
            ok.add(t, 65536)
            late.add(t + (0.15 if 400 < k < 410 else 0.0), 65536)   # a stalled process, data waited
            lossy.add(t + (k - 200) * step * 0.1 if k > 200 else t, 65536)  # from k=200 on: 10 % of the time lost
        self.assertLess(ok.lost_s, 0.005)
        self.assertLess(late.lost_s, 0.02)
        self.assertGreater(lossy.lost_s, 0.8)


class FixedLanguageTest(unittest.TestCase):
    def _run(self, fixed: str, speech_s: float = 2.0):
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest import mock
        from falconsdr.config import TranscriptionConfig
        from falconsdr.playback import transcription_engine as te
        seen: dict = {"detect": 0, "language": None}

        def transcribe(audio, **kw):
            seen["language"] = kw.get("language")
            return [], SimpleNamespace(duration_after_vad=0.0)

        def detect(**kw):
            seen["detect"] += 1
            return "pl", 0.9, [("pl", 0.9)]
        model = SimpleNamespace(transcribe=transcribe, detect_language=detect)
        audio_mod = ModuleType("faster_whisper.audio")
        audio_mod.decode_audio = lambda path: "audio"
        with mock.patch.dict(sys.modules, {"faster_whisper": ModuleType("faster_whisper"), "faster_whisper.audio": audio_mod}), \
                mock.patch.object(te, "_get_models", lambda config: (model, None)), \
                mock.patch.object(te, "_speech_seconds", lambda audio: speech_s):
            te.transcribe_recording(SimpleNamespace(path="x.wav", duration_sec=5.0), TranscriptionConfig(fixed_language=fixed))
        return seen

    def test_empty_means_detect_as_before(self) -> None:
        self.assertEqual(self._run(""), {"detect": 1, "language": "pl"})

    def test_a_fixed_language_skips_detection(self) -> None:
        self.assertEqual(self._run(" PL "), {"detect": 0, "language": "pl"})

    def test_a_language_change_does_not_reload_the_models(self) -> None:
        from falconsdr.config import TranscriptionConfig
        from falconsdr.playback import transcription_engine as te
        base = TranscriptionConfig()
        self.assertEqual(te._model_key(base), te._model_key(TranscriptionConfig(fixed_language="pl", min_duration_sec=2.0)))
        self.assertNotEqual(te._model_key(base), te._model_key(TranscriptionConfig(generic_model="small")))


class NoSpeechPreCheckTest(unittest.TestCase):
    def _run(self, speech_s):
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest import mock
        from falconsdr.config import TranscriptionConfig
        from falconsdr.playback import transcription_engine as te
        calls: list[str] = []

        def transcribe(audio, **kw):
            calls.append("transcribe")
            return [], SimpleNamespace(duration_after_vad=0.0)

        model = SimpleNamespace(
            transcribe=transcribe,
            detect_language=lambda **kw: calls.append("detect") or ("en", 0.9, [("en", 0.9)]),
        )
        audio_mod = ModuleType("faster_whisper.audio")
        audio_mod.decode_audio = lambda path: "audio"

        def speech(_audio):
            if isinstance(speech_s, Exception):
                raise speech_s
            return speech_s
        with mock.patch.dict(sys.modules, {"faster_whisper": ModuleType("faster_whisper"), "faster_whisper.audio": audio_mod}), \
                mock.patch.object(te, "_get_models", lambda config: (model, None)), \
                mock.patch.object(te, "_speech_seconds", speech):
            result = te.transcribe_recording(SimpleNamespace(path="x.wav", duration_sec=5.0), TranscriptionConfig())
        return result, calls

    def test_a_clip_without_speech_never_reaches_the_encoder(self) -> None:
        result, calls = self._run(0.1)
        self.assertEqual((result.status, calls), ("no_speech", []))

    def test_a_clip_with_speech_is_detected_and_transcribed(self) -> None:
        _result, calls = self._run(2.0)
        self.assertEqual(calls, ["detect", "transcribe"])

    def test_when_the_vad_itself_fails_the_normal_path_decides(self) -> None:
        _result, calls = self._run(RuntimeError("onnx"))
        self.assertEqual(calls, ["detect", "transcribe"])


class SingleModelTranscriptionTest(unittest.TestCase):
    """New installs use one model (large-v3) for every language; an English model is optional."""

    def _run(self, english_model: str, language: str) -> list[str]:
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest import mock
        from falconsdr.config import TranscriptionConfig
        from falconsdr.playback import transcription_engine as te
        used: list[str] = []

        def fake_load(model_id, config):
            def transcribe(audio, **kw):
                used.append(model_id)
                return [], SimpleNamespace(duration_after_vad=1.0)
            return SimpleNamespace(transcribe=transcribe)

        audio_mod = ModuleType("faster_whisper.audio")
        audio_mod.decode_audio = lambda path: "audio"
        te.unload_models()
        try:
            with mock.patch.dict(sys.modules, {"faster_whisper": ModuleType("faster_whisper"), "faster_whisper.audio": audio_mod}), \
                    mock.patch.object(te, "_load_model", fake_load) as load:
                config = TranscriptionConfig(english_atc_model=english_model)
                te.transcribe_recording(SimpleNamespace(path="x.wav", duration_sec=5.0), config, forced_language=language)
                te._get_models(config)
        finally:
            te.unload_models()
        return used

    def test_default_is_one_model(self) -> None:
        from falconsdr.config import TranscriptionConfig
        self.assertEqual(TranscriptionConfig().english_atc_model, "")
        self.assertEqual(self._run("", "en"), ["large-v3"])
        self.assertEqual(self._run("", "pl"), ["large-v3"])

    def test_english_model_only_gets_english(self) -> None:
        self.assertEqual(self._run("atc-model", "en"), ["atc-model"])
        self.assertEqual(self._run("atc-model", "pl"), ["large-v3"])


class StaticJsTest(unittest.TestCase):
    def test_no_code_hidden_after_a_line_comment(self) -> None:
        """`stmt; // note io.disconnect(); clearInterval(t);` is valid JS and silently drops the rest
        (once left the Recordings page polling the server forever, ~3 requests/s after a few visits)."""
        import re
        root = Path(__file__).resolve().parent.parent / "falconsdr" / "web" / "static"
        bad = []
        for path in root.rglob("*.js"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("//", 1)
                if len(code) == 2 and "://" not in line and re.search(r"\w+\([^)]*\);\s+\w[\w.]*\(.*\);\s*$", code[1]):
                    bad.append(f"{path.name}:{number}")
        self.assertEqual(bad, [])


class IqUncoveredTimeTest(unittest.TestCase):
    """The acquisition thread counts wall time that no IQ covers: reads are synchronous, so the
    time between two reads is time the dongle drops samples (found while hunting audible gaps)."""

    def test_ignores_reads_while_the_backend_streams(self) -> None:
        import numpy as np
        from falconsdr.sdr_backend import AcquisitionThread

        class Fake:
            sample_rate, label, gain_range_db, streaming = 100_000.0, "fake", (0.0, 50.0), True

            def read(self, n):
                time.sleep(0.030)  # a slow cycle that would count as lost time if it were not a stream
                return np.zeros(n, dtype=np.complex64)

            def close(self):
                pass

        thread = AcquisitionThread(Fake(), 1000, debug=False)
        try:
            for _ in range(10):
                thread.read(1000)
            self.assertEqual((thread._iq_lost_s, thread._iq_streamed_s), (0.0, 0.0))
        finally:
            thread.close()

    def test_counts_a_slow_cycle_but_not_a_retune(self) -> None:
        import numpy as np
        from falconsdr.sdr_backend import AcquisitionThread
        calls, settle = [0], [False]

        class Fake:
            sample_rate, label, gain_range_db = 100_000.0, "fake", (0.0, 50.0)  # 1000 samples = 10 ms

            def read(self, n):
                calls[0] += 1
                slow = calls[0] == 8 or settle[0]  # one hiccup, and the slow 1st read after the retune
                settle[0] = False
                time.sleep(0.010 + (0.080 if slow else 0.0))
                return np.zeros(n, dtype=np.complex64)

            def set_frequency(self, hz):
                time.sleep(0.150)  # a retune stalls the dongle on purpose
                settle[0] = True

            def close(self):
                pass

        thread = AcquisitionThread(Fake(), 1000, debug=False)
        try:
            for _ in range(14):
                thread.read(1000)
            thread.set_frequency(131_900_000)
            for _ in range(12):
                thread.read(1000)
            self.assertGreaterEqual(thread._iq_lost_s, 0.07)   # the hiccup...
            self.assertLess(thread._iq_lost_s, 0.15)           # ...but neither the retune nor its slow first read
        finally:
            thread.close()


class DumpsChunkedTest(unittest.TestCase):
    def test_same_json_as_dumps_for_the_caches(self) -> None:
        from falconsdr.config import dumps_chunked
        data = {"a|1|2": {"text": "zażółć gęślą", "n": 1.5, "ok": True, "x": None}, "b": {}, 'q"uote': {"t": "\n"}}
        for kwargs in ({}, {"ensure_ascii": False}):
            self.assertEqual(json.loads(dumps_chunked(data, **kwargs)), data)
        self.assertEqual(dumps_chunked({}), "{}")


class AudioAnalysisStreamingTest(unittest.TestCase):
    """A recording can last hours; analysis reads it block by block (an 850 MB one was loaded
    whole, took ~8 GB and got the server OOM-killed on every start)."""

    def _wav(self, frames: int, channels: int = 1) -> Path:
        import wave
        import numpy as np
        rng = np.random.default_rng(3)
        x = (rng.normal(0, 2000, frames * channels) * np.repeat(1 + (np.arange(frames) // 3000 % 5), channels)).clip(-32768, 32767)
        x[5000:5400] = 32767  # some clipping
        path = Path(tempfile.mkdtemp()) / "t.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(channels); w.setsampwidth(2); w.setframerate(12500)
            w.writeframes(x.astype("<i2").tobytes())
        return path

    def test_block_size_does_not_change_the_result(self) -> None:
        from unittest import mock
        from falconsdr.playback import audio_analysis as aa
        for channels in (1, 2):
            path = self._wav(70_001, channels)
            whole = (aa.analyze_wav(path), aa.compute_waveform_peaks(path, 400))
            with mock.patch.object(aa, "BLOCK_FRAMES", 1300), mock.patch.object(aa, "PEAK_RESOLUTION", 70_001):
                blocks = (aa.analyze_wav(path), aa.compute_waveform_peaks(path, 400))
            self.assertEqual(whole[0], blocks[0])
            self.assertEqual(whole[1].tolist(), blocks[1].tolist())
            self.assertGreater(whole[0].clipping_ratio, 0)
            self.assertAlmostEqual(whole[0].duration_sec, 70_001 / 12500)

    def test_mp3_is_decoded_to_a_temp_file_that_is_removed(self) -> None:
        from falconsdr.audio_transcode import ffmpeg_exe, transcode_to_mp3
        from falconsdr.playback import audio_analysis as aa
        from falconsdr.playback.config import APP_DATA_DIR
        if ffmpeg_exe() is None:
            self.skipTest("no ffmpeg")
        mp3 = transcode_to_mp3(self._wav(25_000))
        self.assertIsNotNone(mp3)
        quality, peaks = aa.analyze_wav(mp3), aa.compute_waveform_peaks(mp3, 400)
        self.assertTrue(quality.readable)
        self.assertAlmostEqual(quality.duration_sec, 2.0, delta=0.15)
        self.assertEqual(peaks.size, 400)
        self.assertEqual(list(APP_DATA_DIR.glob("decode-*.wav")), [])  # nothing left behind
        broken = Path(tempfile.mkdtemp()) / "x.mp3"
        broken.write_bytes(b"not an mp3")
        self.assertFalse(aa.analyze_wav(broken).readable)
        self.assertEqual(list(APP_DATA_DIR.glob("decode-*.wav")), [])  # nor after a failed decode

    def test_only_one_block_is_in_memory(self) -> None:
        from unittest import mock
        from falconsdr.playback import audio_analysis as aa
        path = self._wav(70_001)
        with mock.patch.object(aa, "BLOCK_FRAMES", 1300):
            sizes = [len(block) for block, *_ in aa._stream_samples(path, aa.WINDOW_SEC)]
        self.assertLessEqual(max(sizes), 1300)
        self.assertEqual(sum(sizes), 70_001)


class SoftwareAgcTest(unittest.TestCase):
    """Fake clock, one peak per 30 ms tick like the scan loop."""

    def _run(self, peaks_db, seconds, gain=40.0):
        from unittest import mock
        from falconsdr.agc import SoftwareAgc
        clock = [1000.0]
        with mock.patch("falconsdr.agc.time.monotonic", lambda: clock[0]):
            agc, steps = SoftwareAgc(gain), []
            for i in range(int(seconds / 0.03)):
                clock[0] += 0.03
                new = agc.step(10 ** (peaks_db(i, agc.gain_db) / 20.0), (0.0, 49.6))
                if new is not None:
                    steps.append(round(clock[0] - 1000.0, 2))
        return steps, agc

    def test_noisy_but_steady_level_does_not_step(self) -> None:
        # Target -6 dBFS; single-chunk peaks swing between -1 and -11 dBFS around it.
        steps, _ = self._run(lambda i, g: -6.0 + (5.0 if i % 2 else -5.0), 20)
        self.assertEqual(steps, [])

    def test_sustained_error_is_corrected_within_about_a_second_each_step(self) -> None:
        # The station really is 10 dB too hot at gain 40: ends near target, in steps >= 1 s apart.
        steps, agc = self._run(lambda i, g: -6.0 + 10.0 + (g - 40.0), 8)
        self.assertTrue(steps and all(b - a >= 1.0 for a, b in zip(steps, steps[1:])))
        self.assertLess(agc.gain_db, 40.0)
        self.assertAlmostEqual(agc.gain_db, 30.0, delta=3.5)

    def test_near_full_scale_peak_is_acted_on_at_once(self) -> None:
        steps, _ = self._run(lambda i, g: -0.2, 1.2)  # clipping from the start
        self.assertTrue(steps and steps[0] <= 1.05)


class LiveBandOverrideTest(unittest.TestCase):
    def test_open_window_sees_a_patched_band(self) -> None:
        """merge_patch replaces config.bands with new objects; the window an
        open transmission sits on must still pick up the edited trigger."""
        from types import SimpleNamespace
        from falconsdr.config import VfoRangeConfig
        from falconsdr.controller import merge_patch
        from falconsdr.scanner_engine import ScannerEngine
        cfg = AppConfig()
        cfg.bands = [VfoRangeConfig(id="b1", trigger_level_dbfs=-47.0)]
        engine = SimpleNamespace(config=cfg)
        win = SimpleNamespace(band=cfg.bands[0])  # as _rebuild_windows() tags it
        self.assertEqual(ScannerEngine._effective_trigger_dbfs(engine, win), -47.0)
        merge_patch(cfg, {"bands": [{"id": "b1", "trigger_level_dbfs": -60.0}]})
        self.assertEqual(ScannerEngine._effective_trigger_dbfs(engine, win), -60.0)
        merge_patch(cfg, {"bands": []})  # band deleted meanwhile: keep what the window has
        self.assertEqual(ScannerEngine._effective_trigger_dbfs(engine, win), -47.0)

    def test_power_of_a_dropped_frequency_does_not_raise(self) -> None:
        import numpy as np
        from falconsdr.channelizer import channel_powers_from_spectrum
        bins = np.linspace(-1e6, 1e6, 2001)
        spectrum = (bins, np.concatenate(([0.0], np.cumsum(np.ones(2000)))))
        out = channel_powers_from_spectrum(spectrum, 100e6, [100_000_000, 100_050_000], {100_000_000: 12_500.0})
        self.assertEqual(set(out), {100_000_000, 100_050_000})


class AmBlockEdgeTest(unittest.TestCase):
    """Demodulating in 30 ms blocks must equal demodulating the stream in one
    go - a per-block resample_poly sagged the AM envelope at every block edge
    (a 33 Hz buzz)."""

    def test_blockwise_equals_whole(self):
        import numpy as np
        from falconsdr.dsp import Demodulator
        from falconsdr.models import Modulation
        fs, chunk = 2_400_000, 72_000
        t = np.arange(fs) / fs
        iq = (0.3 * (1 + 0.5 * np.sin(2 * np.pi * 1000 * t))).astype(np.complex64)
        whole = np.abs(Demodulator(fs)._decimate(iq))
        d = Demodulator(fs)
        blocks = np.abs(np.concatenate([d._decimate(iq[i:i + chunk]) for i in range(0, fs, chunk)]))
        self.assertLess(np.abs(whole[1000:11000] - blocks[1000:11000]).max(), 1e-3)


class DigitalVoiceTest(unittest.TestCase):
    def test_stream_resampler_matches_one_call_on_the_whole_stream(self) -> None:
        import numpy as np
        from scipy.signal import resample_poly
        from falconsdr.dsp import StreamResampler
        rng = np.random.default_rng(1)
        for fs in (2_400_000, 2_048_000):
            x = (rng.standard_normal(fs // 4) + 1j * rng.standard_normal(fs // 4)).astype(np.complex64)
            g = np.gcd(48000, fs)
            whole = resample_poly(x, 48000 // g, fs // g)
            r, parts, i = StreamResampler(fs, 48000), [], 0
            while i < x.size:  # odd block sizes, some shorter than the filter
                n = int(rng.integers(300, 80000))
                parts.append(r.process(x[i:i + n]))
                i += n
            y = np.concatenate(parts)
            self.assertGreater(y.size, whole.size - 50)  # only the last few samples still wait for more input
            np.testing.assert_allclose(y, whole[:y.size], atol=1e-5)

    def test_decoder_parses_calls_and_keeps_its_timeline(self) -> None:
        import numpy as np
        from falconsdr import digital_voice
        dec = digital_voice.DsdDecoder.__new__(digital_voice.DsdDecoder)  # no dsd-fme process
        dec.out_rate, dec._frac, dec._buf, dec._lock = 12500, 0.0, np.zeros(0, np.float32), threading.Lock()
        dec.talkgroup = dec.source = ""
        dec.encrypted = False
        dec._proc = type("P", (), {"stderr": [b"12:00 Sync: +DMR  slot1 | TGT=260 SRC=2601234 Group\n",
                                              b"Encrypted call\n"]})()
        dec._read_info()
        self.assertEqual((dec.talkgroup, dec.source, dec.encrypted), ("260", "2601234", True))
        dec._proc = type("P", (), {"stderr": [b"Sync: +YSF  DST: ALL        SRC: SP8ABC     \n"]})()
        dec._read_info()
        self.assertEqual((dec.talkgroup, dec.source), ("ALL", "SP8ABC"))
        # Silence while nothing is decoded, then voice - always exactly n samples per call.
        self.assertEqual(dec.read(375).tolist(), [0.0] * 375)
        dec._buf = np.full(800, 0.5, np.float32)
        out = np.concatenate([dec.read(375) for _ in range(10)])
        self.assertEqual(out.size, 3750)
        self.assertAlmostEqual(float(out[:1000].mean()), 0.5, places=3)  # 800 samples @ 8 kHz = 1250 @ 12.5 kHz


    def test_download_retries_only_certificate_errors_with_other_trust_stores(self) -> None:
        import ssl
        import urllib.error
        from unittest import mock
        from falconsdr import digital_voice
        seen = []

        def fake(url, timeout, context):
            seen.append(context)
            if context is None:  # the default store doesn't know the issuer
                raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get issuer certificate"))
            return "response"

        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(digital_voice._urlopen("https://example.invalid/x"), "response")
        self.assertEqual(len(seen), 2)  # default, then the OS verifier
        seen.clear()

        def offline(url, timeout, context):
            seen.append(context)
            raise urllib.error.URLError(OSError("network unreachable"))

        with mock.patch("urllib.request.urlopen", offline), self.assertRaises(urllib.error.URLError):
            digital_voice._urlopen("https://example.invalid/x")
        self.assertEqual(seen, [None])  # not a certificate problem - no second try


class DigitalDownloadChecksumTest(unittest.TestCase):
    def _serve_bytes(self, data: bytes):
        import io
        from unittest import mock
        from falconsdr import digital_voice
        return digital_voice, mock.patch.object(digital_voice, "_urlopen", lambda url: io.BytesIO(data))

    def test_a_download_that_is_not_the_tested_one_is_removed_and_refused(self) -> None:
        import gzip, hashlib
        payload = b"tar-bytes"
        dv, patch = self._serve_bytes(gzip.compress(payload))
        dest = Path(tempfile.mkdtemp(dir=_HOME)) / "a.tar.gz"
        with patch:
            dv._download("https://github.com/x/y/z", dest, hashlib.sha256(payload).hexdigest(), gunzip=True)
            self.assertTrue(dest.exists())  # right bytes, even if re-compressed
            with self.assertRaises(RuntimeError):
                dv._download("https://github.com/x/y/z", dest, "0" * 64, gunzip=True)
        self.assertFalse(dest.exists())


class RtlSdrListTest(unittest.TestCase):
    def test_every_dongle_is_listed_and_shared_serials_go_by_usb_order(self) -> None:
        import sys
        import types
        from unittest import mock
        from falconsdr import sdr_backend
        serials = ["SDR-A", "00000001", "00000001", None]  # None: in use / no WinUSB driver

        def read(_lib, i):
            if serials[i] is None:
                raise OSError("busy")
            return serials[i]

        lib = types.SimpleNamespace(rtlsdr_get_device_count=lambda: len(serials))
        fake = types.ModuleType("rtlsdr.librtlsdr")
        fake.librtlsdr = lib
        with mock.patch.dict(sys.modules, {"rtlsdr": types.ModuleType("rtlsdr"), "rtlsdr.librtlsdr": fake}), \
                mock.patch.object(sdr_backend, "HAVE_RTLSDR_NATIVE", True), \
                mock.patch.object(sdr_backend, "_read_serial", read):
            devs = sdr_backend.RtlSdrBackend.enumerate_devices()
        self.assertEqual(len(devs), 4)  # one unreadable dongle no longer hides the others
        self.assertEqual(devs[0]["serial"], "SDR-A")  # unique serial: stable across replugging
        self.assertNotIn("index", devs[0])
        self.assertEqual([d.get("index") for d in devs[1:]], ["1", "2", "3"])
        self.assertTrue(all("," not in d["label"] for d in devs))  # Controller.devices() joins with commas


class BiasTeeTest(unittest.TestCase):
    """Bias-T powers the antenna: on only when asked, only once the device is
    fully set up, re-asserted on retune, and always off again on close."""

    def test_native_rtlsdr(self) -> None:
        from unittest import mock
        from falconsdr import sdr_backend
        calls = []

        class FakeRtl:
            fail_gains = False

            def __init__(self, **_kw):
                pass

            def set_bias_tee(self, on):
                calls.append(on)

            def get_gains(self):
                if FakeRtl.fail_gains:
                    raise OSError("usb gone")
                return [0, 496]

            def set_gain(self, _g):
                pass

            def close(self):
                calls.append("close")

        with mock.patch.object(sdr_backend, "HAVE_RTLSDR_NATIVE", True), \
                mock.patch.object(sdr_backend, "RtlSdr", FakeRtl, create=True), \
                mock.patch.object(sdr_backend.SoapyBackend, "_wait_for_reopen_cooldown", staticmethod(lambda: None)), \
                mock.patch.object(sdr_backend.SoapyBackend, "_record_close_time", staticmethod(lambda: None)):
            b = sdr_backend.RtlSdrBackend("", 2_400_000, "manual", 30.0)
            self.assertNotIn(True, calls)  # default: never on
            b.close()
            calls.clear()
            b = sdr_backend.RtlSdrBackend("", 2_400_000, "manual", 30.0, bias_tee=True)
            self.assertEqual(calls, [False, True])  # off while setting up, on at the very end
            b.set_frequency(121_500_000)
            self.assertEqual(calls[-1], True)
            b.close()
            self.assertEqual(calls[-2:], [False, "close"])
            calls.clear()
            FakeRtl.fail_gains = True  # a failed open must never leave it powered
            with self.assertRaises(OSError):
                sdr_backend.RtlSdrBackend("", 2_400_000, "manual", 30.0, bias_tee=True)
            self.assertNotIn(True, calls)

    def test_soapy(self) -> None:
        from types import SimpleNamespace
        from unittest import mock
        from falconsdr import sdr_backend
        writes = []

        class FakeDevice:
            def __init__(self, _args):
                pass

            def writeSetting(self, k, v):
                writes.append((k, v))

            def __getattr__(self, name):  # every other Soapy call: accept and do nothing
                return lambda *a, **k: SimpleNamespace(minimum=lambda: 0.0, maximum=lambda: 49.6)

        soapy = SimpleNamespace(Device=FakeDevice)
        with mock.patch.object(sdr_backend, "HAVE_SOAPY", True), \
                mock.patch.object(sdr_backend, "SoapySDR", soapy, create=True), \
                mock.patch.object(sdr_backend, "SOAPY_SDR_RX", 0, create=True), \
                mock.patch.object(sdr_backend, "SOAPY_SDR_CF32", "CF32", create=True), \
                mock.patch.object(sdr_backend, "SdrIoDiagLogger", lambda: None), \
                mock.patch.object(sdr_backend.SoapyBackend, "_wait_for_reopen_cooldown", staticmethod(lambda: None)), \
                mock.patch.object(sdr_backend.SoapyBackend, "_record_close_time", staticmethod(lambda: None)):
            b = sdr_backend.SoapyBackend("rtlsdr", "biastee=true", 2_400_000, "manual", 30.0)
            self.assertNotIn("true", [v for _k, v in writes])  # neither by default nor smuggled in via device_args
            b.close()
            writes.clear()
            b = sdr_backend.SoapyBackend("rtlsdr", "", 2_400_000, "manual", 30.0, bias_tee=True)
            self.assertEqual(writes[-2:], [("biastee", "true"), ("bias_tee", "true")])
            self.assertNotIn(("offset_tuning", "true"), writes)
            writes.clear()
            b.set_frequency(121_500_000)
            self.assertEqual(writes, [("biastee", "true"), ("bias_tee", "true")])
            b.close()
            self.assertEqual(writes[-3:], [("biastee", "false"), ("bias_tee", "false"), ("offset_tuning", "false")])


class MultiDongleTest(unittest.TestCase):
    def test_scan_plan_is_split_without_overlap_and_lanes_keep_their_weight(self) -> None:
        from falconsdr.channelizer import plan_windows
        from falconsdr.scanner_engine import _Lane, _partition_windows
        freqs = list(range(118_000_000, 137_000_000, 25_000))
        default = plan_windows(freqs[:500], 2_400_000, 25_000)
        lane = _Lane(weight=2.0, windows=plan_windows(freqs[600:], 2_400_000, 25_000), is_range=True)
        everything = {w.center_hz for w in default} | {w.center_hz for w in lane.windows}
        shares = [_partition_windows(default, [lane], k, 3) for k in range(3)]
        seen = [{w.center_hz for w in d} | {w.center_hz for ln in lanes for w in ln.windows} for d, lanes in shares]
        self.assertEqual(set().union(*seen), everything)  # nothing left unscanned
        self.assertEqual(sum(len(s) for s in seen), len(everything))  # no window scanned twice
        self.assertLessEqual(max(map(len, seen)) - min(map(len, seen)), 1)  # even split
        for _d, lanes in shares:
            self.assertTrue(all(ln.weight == 2.0 and ln.is_range for ln in lanes))
        self.assertEqual(_partition_windows(default[:1], [], 1, 2), ([], []))  # more dongles than windows: idle

    def test_one_transmission_is_claimed_by_one_dongle(self) -> None:
        from falconsdr.scanner_engine import SharedFrequencies
        shared, a, b = SharedFrequencies(), object(), object()
        self.assertTrue(shared.claim(121_500_000, a))
        self.assertFalse(shared.claim(121_500_000, b))
        self.assertTrue(shared.taken_by_other(121_500_000, b))
        self.assertFalse(shared.taken_by_other(121_500_000, a))
        shared.release(121_500_000, b)  # not b's - stays a's
        self.assertTrue(shared.taken_by_other(121_500_000, b))
        shared.release_all(a)
        self.assertTrue(shared.claim(121_500_000, b))

    def test_live_status_merges_dongles_and_audio_follows_the_one_heard(self) -> None:
        from falconsdr.controller import Controller
        ctl = Controller(AppConfig())
        heard = []
        ctl._audio_subscribers = [type("Q", (), {"put_nowait": lambda self, pcm: heard.append(pcm)})()]
        idle = ctl._idle_live()
        ctl._lives = [{**idle, "state": "SCANNING", "frequencies": [[118_000_000, "window"]]},
                      {**idle, "state": "SIGNAL_DETECTED", "frequencies": [[124_000_000, "Tower"]], "recording": True}]
        ctl._engines = [object(), object()]
        ctl._audible = 1
        ctl._on_live_audio(0, [0.1])  # dongle 2 is mid-transmission - dongle 1 can't take the speaker
        self.assertEqual(len(heard), 0)
        ctl._on_live_audio(1, [0.1])
        self.assertEqual(len(heard), 1)
        with ctl._lock:
            live = ctl._merged_live_locked()
        self.assertEqual(live["state"], "SIGNAL_DETECTED")
        self.assertEqual(live["frequencies"][0][0], 124_000_000)
        self.assertTrue(live["recording"])
        self.assertEqual(len(live["dongles"]), 2)
        ctl._lives[1]["state"] = "SCANNING"  # its transmission ended - the next dongle that hears something takes over
        ctl._on_live_audio(0, [0.1])
        self.assertEqual(ctl._audible, 0)
        ctl._engines = []


class AntennaRangeTest(unittest.TestCase):
    def _plan(self, spans):
        from falconsdr.channelizer import plan_windows
        return [w for lo, hi in spans for w in plan_windows(list(range(lo, hi, 25_000)), 2_400_000, 25_000)]

    def test_no_limit_by_default_and_ends_are_normalised(self) -> None:
        from falconsdr.antenna_range import OPEN_END_HZ, device_range_hz, device_ranges, is_limited
        cfg = AppConfig()
        self.assertEqual(device_ranges(cfg), [(0, OPEN_END_HZ)])
        self.assertFalse(is_limited(device_ranges(cfg)))
        self.assertEqual(device_range_hz({"range_start_hz": 146_000_000, "range_stop_hz": 144_000_000}), (144_000_000, 146_000_000))
        self.assertEqual(device_range_hz({"range_start_hz": 430_000_000}), (430_000_000, OPEN_END_HZ))
        cfg.device.extra_devices = [{"driver": "rtlsdr", "range_stop_hz": 146_000_000}, {"device_args": "no driver"}]
        self.assertEqual(len(device_ranges(cfg)), 2)  # same filter as Controller._devices()

    def test_each_dongle_scans_only_its_range_and_every_frequency_once(self) -> None:
        from falconsdr.antenna_range import split_by_range
        m2, cm70 = (144_000_000, 146_000_000), (430_000_000, 440_000_000)
        plan = self._plan([(144_000_000, 146_000_000), (430_000_000, 440_000_000), (118_000_000, 120_000_000)])
        ranges = [m2, cm70]
        shares = [split_by_range(list(plan), [], k, ranges)[0] for k in range(2)]
        freqs = [sorted(f for w in share for f in w.freqs) for share in shares]
        self.assertTrue(freqs[0] and all(m2[0] <= f <= m2[1] for f in freqs[0]))
        self.assertTrue(freqs[1] and all(cm70[0] <= f <= cm70[1] for f in freqs[1]))
        wanted = {f for w in plan for f in w.freqs if m2[0] <= f <= m2[1] or cm70[0] <= f <= cm70[1]}
        self.assertEqual(set(freqs[0]) | set(freqs[1]), wanted)  # 118-120 MHz is in nobody's range: dropped
        for share in shares:  # a window trimmed to the range is re-centred on what is left
            for w in share:
                self.assertTrue(min(w.freqs) <= w.center_hz <= max(w.freqs))

    def test_overlapping_ranges_take_turns_and_unlimited_dongle_gets_the_rest(self) -> None:
        from falconsdr.antenna_range import OPEN_END_HZ, split_by_range
        plan = self._plan([(144_000_000, 150_000_000), (430_000_000, 432_000_000)])
        ranges = [(144_000_000, 150_000_000), (0, OPEN_END_HZ)]
        a = split_by_range(list(plan), [], 0, ranges)[0]
        b = split_by_range(list(plan), [], 1, ranges)[0]
        fa = {f for w in a for f in w.freqs}
        fb = {f for w in b for f in w.freqs}
        self.assertFalse(fa & fb)  # nothing scanned twice
        self.assertEqual(fa | fb, {f for w in plan for f in w.freqs})  # nothing lost
        self.assertTrue(all(f < 150_000_000 for f in fa) and any(f > 430_000_000 for f in fb))
        self.assertTrue(any(f < 150_000_000 for f in fb))  # the unlimited one helps inside the overlap

    def test_lane_keeps_weight_and_empty_lane_disappears(self) -> None:
        from falconsdr.antenna_range import split_by_range
        from falconsdr.scanner_engine import _Lane
        lane = _Lane(weight=3.0, windows=self._plan([(430_000_000, 431_000_000)]), is_range=True)
        _d, mine = split_by_range([], [lane], 1, [(144_000_000, 146_000_000), (430_000_000, 440_000_000)])
        _d, other = split_by_range([], [lane], 0, [(144_000_000, 146_000_000), (430_000_000, 440_000_000)])
        self.assertEqual([(ln.weight, ln.is_range) for ln in mine], [(3.0, True)])
        self.assertEqual(other, [])

    def test_uncovered_is_reported_only_when_every_dongle_is_limited(self) -> None:
        from falconsdr.antenna_range import uncovered
        from falconsdr.config import VfoRangeConfig
        from falconsdr.models import Channel
        cfg = AppConfig()
        cfg.device.range_start_hz, cfg.device.range_stop_hz = 144_000_000, 146_000_000
        airband = VfoRangeConfig(label="Airband", start_hz=118_000_000, stop_hz=136_000_000)
        two_m = VfoRangeConfig(label="2 m", start_hz=144_000_000, stop_hz=148_000_000)
        channels = [Channel(freq_hz=145_500_000, name="in"), Channel(freq_hz=433_000_000, name="out"),
                    Channel(freq_hz=434_000_000, name="off", enabled=False)]
        items = uncovered(cfg, channels, [airband, two_m], True)
        self.assertEqual([i["kind"] for i in items], ["band", "band", "channels"])
        self.assertEqual(items[0]["gaps"], [[118_000_000, 136_000_000]])
        self.assertEqual(items[1]["gaps"], [[146_000_001, 148_000_000]])  # partly covered: only the rest
        self.assertEqual((items[2]["count"], items[2]["examples"]), (1, [433_000_000]))
        self.assertEqual(uncovered(cfg, channels, [], False), [])
        cfg.device.extra_devices = [{"driver": "rtlsdr", "range_start_hz": 430_000_000, "range_stop_hz": 440_000_000}]
        self.assertEqual([i["kind"] for i in uncovered(cfg, channels, [airband], True)], ["band"])  # 70 cm channel now covered
        cfg.device.extra_devices = [{"driver": "rtlsdr"}]  # an unlimited second dongle reaches everything
        self.assertEqual(uncovered(cfg, channels, [airband], True), [])

    def test_controller_puts_the_uncovered_list_in_the_live_status(self) -> None:
        from falconsdr.controller import Controller
        cfg = AppConfig()
        ctl = Controller(cfg)
        self.assertEqual(ctl.live()["uncovered"], [])
        cfg.device.range_start_hz, cfg.device.range_stop_hz = 430_000_000, 440_000_000
        self.assertTrue(ctl.live()["uncovered"])  # the default 118-136 MHz band is outside the 70 cm antenna


class ScheduleTest(unittest.TestCase):
    def test_active_entries_pick_bands_and_survive_a_config_patch(self) -> None:
        import datetime as dt
        from falconsdr import schedule
        from falconsdr.config import ScheduleEntry, VfoRangeConfig
        from falconsdr.controller import merge_patch
        cfg = AppConfig()
        day, night = VfoRangeConfig(label="day", enabled=False), VfoRangeConfig(label="night")
        cfg.bands = [day, night]
        cfg.memory_list_enabled = True
        cfg.schedule.entries = [
            ScheduleEntry(start="08:00", end="16:00", band_ids=[day.id]),
            ScheduleEntry(start="22:00", end="06:00", band_ids=[night.id], memory_list=True),  # crosses midnight
        ]
        at = lambda hh, mm: dt.datetime(2026, 1, 1, hh, mm)
        self.assertEqual(schedule.scan_selection(cfg, at(9, 0)), ([night], True))  # off: the bands' own switches
        cfg.schedule.enabled = True
        self.assertEqual(schedule.scan_selection(cfg, at(9, 0)), ([day], False))
        self.assertEqual(schedule.scan_selection(cfg, at(16, 0)), ([], False))   # end is exclusive, gaps scan nothing
        self.assertEqual(schedule.scan_selection(cfg, at(2, 30)), ([night], True))
        cfg.schedule.entries[0].end = "08:00"                                     # start == end: all day
        self.assertEqual(schedule.scan_selection(cfg, at(23, 0)), ([day, night], True))
        # Weekdays (2026-01-02 is a Friday): a range crossing midnight belongs to its start day.
        cfg.schedule.entries = [ScheduleEntry(start="22:00", end="06:00", days=[4], band_ids=[night.id])]
        fri, sat, sun = (lambda hh, d=d: dt.datetime(2026, 1, d, hh) for d in (2, 3, 4))
        self.assertEqual(schedule.scan_selection(cfg, fri(23))[0], [night])
        self.assertEqual(schedule.scan_selection(cfg, sat(5))[0], [night])   # Friday's night, continued
        self.assertEqual(schedule.scan_selection(cfg, sat(23))[0], [])
        self.assertEqual(schedule.scan_selection(cfg, sun(5))[0], [])
        cfg.schedule.outside = "normal"                                       # between ranges: the usual switches
        self.assertEqual(schedule.scan_selection(cfg, sat(23)), ([night], True))
        merge_patch(cfg, {"schedule": {"entries": [{"start": "07:00", "end": "09:00", "band_ids": [day.id]}]},
                          "bands": [{"id": day.id, "label": "day"}]})
        self.assertEqual(cfg.bands[0].id, day.id)
        self.assertEqual(schedule.scan_selection(cfg, at(8, 0))[0][0].id, day.id)


class WorkerMirrorTest(unittest.TestCase):
    def test_reports_only_size_verified_copies(self) -> None:
        from falconsdr.worker import Worker

        class FakeApi:
            base = "http://x"
            mirrored: list[str] = []

            def get(self, _path):
                return [{"id": "d/" + NAME.format(khz=1), "size": 5}, {"id": "d/" + NAME.format(khz=2), "size": 9}]

            def download(self, rel, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"12345")  # the second file arrives short

            def post(self, _path, body, timeout=30):
                self.mirrored += body["ids"]

        root = Path(tempfile.mkdtemp(dir=_HOME))
        w = Worker("http://x", "", local_root=root)
        w.api = FakeApi()
        w._settings = {"mirror": True, "mirror_dir": ""}
        w._mirror()
        self.assertEqual(w.api.mirrored, ["d/" + NAME.format(khz=1)])


class WorkerProgressTest(unittest.TestCase):
    def _worker(self):
        from falconsdr.worker import Worker
        return Worker("http://x", "", local_root=Path(tempfile.mkdtemp(dir=_HOME)))

    def test_a_status_change_asks_for_an_immediate_heartbeat_and_is_timed(self) -> None:
        w = self._worker()
        w._hb_wake.clear()
        w._set_status("installing")
        self.assertTrue(w._hb_wake.is_set())
        self.assertEqual((w._info()["status"], w._info()["status_for_s"]), ("installing", 0))

    def test_every_install_line_is_published_and_kept(self) -> None:
        w = self._worker()
        w._hb_wake.clear()
        w._log_line("Downloading ctranslate2 (38 MB)")
        self.assertTrue(w._hb_wake.is_set())
        self.assertEqual(list(w._log)[-1], "Downloading ctranslate2 (38 MB)")

    def test_a_missing_model_is_reported_as_downloading_until_it_is_on_disk(self) -> None:
        from unittest import mock
        from falconsdr import worker as worker_mod
        from falconsdr.config import TranscriptionConfig
        w = self._worker()
        cached = {"large-v3": False}
        with mock.patch.object(worker_mod, "model_cached", lambda m: cached[m]), \
                mock.patch.object(worker_mod, "model_download_bytes", lambda m: 1_500_000_000):
            w._expect_model_download(TranscriptionConfig(generic_model="large-v3", english_atc_model=""))
            info = w._info()
            self.assertEqual((info["status"], info["download"]), ("downloading", {"model": "large-v3", "bytes": 1_500_000_000}))
            w._expect_model_download(TranscriptionConfig(generic_model="large-v3", english_atc_model=""))
            self.assertEqual(sum("downloading the model" in line for line in w._log), 1)  # said once
            cached["large-v3"] = True
            info = w._info()
            self.assertEqual((info["status"], info["download"]), ("idle", None))

    def test_a_double_click_queues_a_command_once(self) -> None:
        from falconsdr import controller as controller_mod
        cfg = AppConfig()
        cfg.recording.output_dir = tempfile.mkdtemp(dir=_HOME)
        ctl = controller_mod.Controller(cfg)
        ctl.send_worker_command("pc", "install")
        ctl.send_worker_command("pc", "install")
        ctl.send_worker_command("pc", "restart")
        self.assertEqual(ctl.worker_commands["pc"], ["install", "restart"])

    def test_model_cache_probes(self) -> None:
        from falconsdr.playback import transcription_install as ti
        cache = Path(tempfile.mkdtemp(dir=_HOME))
        old = os.environ.get("HF_HUB_CACHE")
        os.environ["HF_HUB_CACHE"] = str(cache)
        try:
            self.assertFalse(ti.model_cached("large-v3"))
            snap = cache / "models--Systran--faster-whisper-large-v3" / "snapshots" / "abc"
            snap.mkdir(parents=True)
            (cache / "models--Systran--faster-whisper-large-v3" / "blobs").mkdir()
            (cache / "models--Systran--faster-whisper-large-v3" / "blobs" / "x.incomplete").write_bytes(b"0" * 1234)
            self.assertFalse(ti.model_cached("large-v3"))  # still downloading
            self.assertEqual(ti.model_download_bytes("large-v3"), 1234)
            (snap / "model.bin").write_bytes(b"w")
            self.assertTrue(ti.model_cached("large-v3"))
            self.assertTrue(ti.model_cached(str(cache)))  # a folder path
        finally:
            if old is None:
                os.environ.pop("HF_HUB_CACHE", None)
            else:
                os.environ["HF_HUB_CACHE"] = old


class WorkerSafeRelTest(unittest.TestCase):
    def test_paths_that_leave_the_folder_are_refused(self) -> None:
        from falconsdr.worker import safe_rel
        self.assertTrue(safe_rel("2026-01-01/" + NAME.format(khz=131000)))
        self.assertFalse(safe_rel("../../x/" + NAME.format(khz=131000)))
        self.assertFalse(safe_rel("/etc/" + NAME.format(khz=131000)))
        self.assertFalse(safe_rel("2026-01-01/.bashrc"))
        if os.name == "nt":  # a rooted or drive-relative path is not "absolute" there, but still leaves the folder
            self.assertFalse(safe_rel("\\etc\\" + NAME.format(khz=131000)))
            self.assertFalse(safe_rel("C:" + NAME.format(khz=131000)))
            self.assertFalse(safe_rel("C:\\x\\" + NAME.format(khz=131000)))

    def test_mirror_skips_a_hostile_id_but_copies_the_rest(self) -> None:
        from falconsdr.worker import Worker
        good, evil = "d/" + NAME.format(khz=1), "../" + NAME.format(khz=2)

        class FakeApi:
            base = "http://x"
            downloaded: list[str] = []

            def get(self, _path):
                return [{"id": good, "size": 5}, {"id": evil, "size": 5}]

            def download(self, rel, target):
                self.downloaded.append(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"12345")

            def post(self, *_a, **_k):
                pass

        root = Path(tempfile.mkdtemp(dir=_HOME)) / "mirror"
        root.mkdir()
        w = Worker("http://x", "", local_root=root)
        w.api = FakeApi()
        w._settings = {"mirror": True, "mirror_dir": ""}
        w._mirror()
        self.assertEqual(w.api.downloaded, [good])
        self.assertFalse((root.parent / NAME.format(khz=2)).exists())


class HttpTest(unittest.TestCase):
    def _serve(self, mode: str, token: str):
        from falconsdr.web.server import App, make_handler
        from http.server import ThreadingHTTPServer

        class Ctl:  # only what the tested routes touch
            library = None

            def live(self, since_log=0):
                return {"ok": True}

            def stop(self):
                pass
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(AppConfig(), Ctl(), token, mode)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def _status(self, url: str, method="GET", headers=None) -> int:
        try:
            with urlopen(Request(url, method=method, data=b"{}" if method == "POST" else None, headers=headers or {}), timeout=5) as r:
                return r.status
        except HTTPError as e:
            e.close()
            return e.code

    def test_password_is_optional_and_enforced_when_set(self) -> None:
        open_url = self._serve("server", "")
        self.assertEqual(self._status(open_url + "/api/live"), 200)
        locked = self._serve("server", "secret")
        self.assertEqual(self._status(locked + "/api/live"), 401)
        self.assertEqual(self._status(locked + "/api/live", headers={"Authorization": "Bearer nope"}), 401)
        self.assertEqual(self._status(locked + "/api/live", headers={"Authorization": "Bearer secret"}), 200)

    def test_an_unexpected_error_is_answered_with_500(self) -> None:
        from falconsdr.web.server import App, make_handler
        from http.server import ThreadingHTTPServer

        class Ctl:
            library = None

            def live(self, since_log=0):
                raise AttributeError("a bug")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(AppConfig(), Ctl(), "", "server")))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.assertEqual(self._status(f"http://127.0.0.1:{httpd.server_address[1]}/api/live"), 500)

    def test_a_wrong_password_is_answered_slowly(self) -> None:
        from unittest import mock
        from falconsdr.web import server
        url = self._serve("server", "secret")
        def login(token: str) -> tuple[int, float]:
            began = time.monotonic()
            req = Request(url + "/api/login", data=json.dumps({"token": token}).encode(), method="POST",
                          headers={"X-FalconSDR": "1", "Content-Type": "application/json"})
            try:
                with urlopen(req) as r:
                    status = r.status
            except HTTPError as exc:
                status = exc.code
            return status, time.monotonic() - began
        with mock.patch.object(server, "_LOGIN_FAIL_DELAY_S", 0.4):
            self.assertEqual(login("secret")[0], 200)
            status, elapsed = login("wrong")
        self.assertEqual(status, 401)
        self.assertGreaterEqual(elapsed, 0.3)  # asked for 0.4; Windows timers are coarse and sleep() can return early

    def test_csrf_and_dns_rebinding_are_refused(self) -> None:
        url = self._serve("local", "")
        self.assertEqual(self._status(url + "/api/scanner/stop", "POST"), 403)
        self.assertEqual(self._status(url + "/api/scanner/stop", "POST", {"X-FalconSDR": "1"}), 200)
        self.assertEqual(self._status(url + "/api/live", headers={"Host": "evil.example:80"}), 403)



class ControllerStartTest(unittest.TestCase):
    def test_two_simultaneous_starts_start_one_set_of_engines(self) -> None:
        from unittest import mock
        from falconsdr import controller as controller_mod
        cfg = AppConfig()
        cfg.recording.output_dir = tempfile.mkdtemp(dir=_HOME)
        ctl = controller_mod.Controller(cfg)
        started = []
        real_connect = ctl._connect
        def slow_connect(*args):
            time.sleep(0.2)  # widen the window between "no engines yet" and "engines stored"
            real_connect(*args)
        with mock.patch.object(controller_mod.ScannerEngine, "start", lambda self: started.append(self)), \
                mock.patch.object(ctl, "_connect", slow_connect):
            threads = [threading.Thread(target=ctl.start) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(started), 1)


@unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset (not Windows)")
class FilenameTimeZoneTest(unittest.TestCase):
    def test_a_filename_is_local_time_of_this_computer_not_of_poland(self) -> None:
        from falconsdr.playback.organizer import parse_filename
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            parsed = parse_filename("2026-01-01_10-00-00_131000.wav")
            self.assertEqual(parsed.dt_local.astimezone(__import__("datetime").timezone.utc).hour, 15)
            os.environ["TZ"] = "UTC"
            time.tzset()
            self.assertEqual(parse_filename("2026-07-01_10-00-00_131000.wav").dt_local.utcoffset().total_seconds(), 0)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()


class WorkerTokenTest(unittest.TestCase):
    def test_api_follows_a_password_changed_while_running(self) -> None:
        from falconsdr.worker import Api
        holder = {"token": ""}
        api = Api("http://x", lambda: holder["token"])
        self.assertEqual(api.token, "")
        holder["token"] = "secret"
        self.assertEqual(api.token, "secret")
        self.assertEqual(Api("http://x", "plain").token, "plain")


class UploadAndRangeTest(_LibCase):
    def test_upload_target_stays_inside_the_library_and_needs_a_recording_name(self) -> None:
        ok = self.lib.upload_target("2026-05-05/" + NAME.format(khz=131500))
        self.assertEqual(ok.parent.parent, self.root.resolve())
        for bad in ("../" + NAME.format(khz=131500), "/etc/" + NAME.format(khz=131500), "2026-05-05/notes.txt"):
            with self.assertRaises(ValueError, msg=bad):
                self.lib.upload_target(bad)

    def test_range_requests_are_answered_or_refused_cleanly(self) -> None:
        from falconsdr.web.server import App, make_handler
        from http.server import ThreadingHTTPServer
        rel = "2026-05-05/" + NAME.format(khz=131525)
        _wav(self.root, rel, size=100)
        self.lib.rescan()

        class Ctl:
            library = self.lib
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(AppConfig(), Ctl(), "", "server")))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/recording?id={rel}"

        def get(rng=None):
            req = Request(url, headers={"Range": rng} if rng else {})
            try:
                with urlopen(req) as r:
                    return r.status, len(r.read())
            except HTTPError as exc:
                return exc.code, 0
        self.assertEqual(get(), (200, 100))
        self.assertEqual(get("bytes=0-9"), (206, 10))
        self.assertEqual(get("bytes=90-"), (206, 10))
        self.assertEqual(get("bytes=-20"), (206, 20))
        self.assertEqual(get("bytes=500-"), (416, 0))
        self.assertEqual(get("bytes=-0"), (416, 0))
        self.assertEqual(get("bytes=abc-"), (400, 0))


class MergePatchTest(unittest.TestCase):
    def _ctl(self):
        from falconsdr import controller as controller_mod
        cfg = AppConfig()
        cfg.recording.output_dir = tempfile.mkdtemp(dir=_HOME)
        return controller_mod.Controller(cfg)

    def test_a_bad_value_applies_nothing(self) -> None:
        ctl = self._ctl()
        before = (ctl.config.trigger_level_dbfs, ctl.config.default_blacklist_margin_hz)
        with self.assertRaises(ValueError):
            ctl.patch_config({"trigger_level_dbfs": -10.0, "default_blacklist_margin_hz": "abc"})
        self.assertEqual((ctl.config.trigger_level_dbfs, ctl.config.default_blacklist_margin_hz), before)

    def test_solo_of_a_band_that_does_not_exist_is_refused(self) -> None:
        ctl = self._ctl()
        with self.assertRaises(ValueError):
            ctl.command("solo", {"band": 99})
        self.assertIsNone(ctl._solo_band)

    def test_null_clears_only_fields_that_may_be_empty(self) -> None:
        ctl = self._ctl()
        ctl.patch_config({"location": {"home_lat": 49.5}})
        ctl.patch_config({"location": {"home_lat": None}})
        self.assertIsNone(ctl.config.location.home_lat)
        with self.assertRaises(ValueError):
            ctl.patch_config({"trigger_level_dbfs": None})
        self.assertIsInstance(ctl.config.trigger_level_dbfs, float)


class CacheKeyTest(_LibCase):
    def _lib_for(self, root: Path) -> Library:
        cfg = AppConfig()
        cfg.recording.output_dir = str(root)
        return Library(cfg, lambda: set(), lambda: {})

    def test_a_star_survives_moving_the_library_folder(self) -> None:
        import shutil
        rel = "2026-02-02/2026-02-02_10-00-00_131375.wav"
        path = _wav(self.root, rel)
        os.utime(path, (1_700_000_000, 1_700_000_000))
        self.lib.rescan()
        self.lib.set_favorite([rel], True)
        moved = self.tmp / "moved"
        shutil.copytree(self.root, moved)  # copy2 keeps the mtime
        lib2 = self._lib_for(moved)
        lib2.rescan()
        self.assertTrue(lib2.get(rel).is_favorite)

    def test_old_absolute_keys_are_converted_once(self) -> None:
        old = f"{self.root}{os.sep}2026-03-03/2026-03-03_10-00-00_131400.wav|100|1700000000"
        self.lib.favorites._keys.add(old)
        self.lib.quality._data[old] = {"x": 1}
        self.lib._migrate_keys()
        new = "2026-03-03/2026-03-03_10-00-00_131400.wav|100|1700000000"
        self.assertIn(new, self.lib.favorites._keys)
        self.assertNotIn(old, self.lib.favorites._keys)
        self.assertIn(new, self.lib.quality._data)

    def test_entries_of_vanished_files_are_pruned_but_not_after_a_scan_that_lost_everything(self) -> None:
        keep, gone = "2026-04-04/" + NAME.format(khz=131425), "2026-04-04/" + NAME.format(khz=131450)
        _wav(self.root, keep)
        _wav(self.root, gone)
        self.lib.rescan()
        self.lib.set_favorite([keep, gone], True)
        gone_key = self.lib.get(gone).key
        (self.root / gone).unlink()
        self.lib.rescan()
        self.assertNotIn(gone_key, self.lib.favorites._keys)
        keep_key = self.lib.get(keep).key
        (self.root / keep).unlink()  # a share that came back empty must not wipe the stars
        self.lib.rescan()
        self.assertIn(keep_key, self.lib.favorites._keys)


class FavoritesBatchTest(_LibCase):
    def test_starring_many_recordings_saves_the_file_once(self) -> None:
        from unittest import mock
        rels = []
        for khz in range(131000, 131020):
            rels.append("2026-01-01/" + NAME.format(khz=khz))
            _wav(self.root, rels[-1])
        self.lib.rescan()
        with mock.patch.object(self.lib.favorites, "save") as save:
            self.lib.set_favorite(rels, True)
        self.assertEqual(save.call_count, 1)
        self.assertTrue(all(self.lib.get(r).is_favorite for r in rels))


class LibraryLockTest(_LibCase):
    def test_a_slow_query_does_not_hold_the_lock_the_scanner_needs(self) -> None:
        from unittest import mock
        for khz in (131000, 131025, 131050):
            _wav(self.root, "2026-01-01/" + NAME.format(khz=khz))
        self.lib.rescan()
        def slow_blob(*_a):
            time.sleep(0.3)
            return "x"
        with mock.patch.object(self.lib, "_blob", slow_blob):
            t = threading.Thread(target=lambda: self.lib.query(q="x"))
            t.start()
            time.sleep(0.05)
            began = time.monotonic()
            self.lib.get("2026-01-01/" + NAME.format(khz=131000))  # add_file() needs the same lock
            waited = time.monotonic() - began
            t.join()
        self.assertLess(waited, 0.5)  # held for the whole query it would be ~0.9 s


class LibraryMaintenanceTest(_LibCase):
    def test_a_failing_step_does_not_end_the_maintenance_thread(self) -> None:
        from unittest import mock
        from falconsdr.playback import index
        calls = []
        def broken() -> None:
            calls.append(1)
            raise TypeError("bad cache entry")
        with mock.patch.object(index, "_SAVE_INTERVAL_S", 0.01), mock.patch.object(self.lib, "save", broken):
            thread = threading.Thread(target=self.lib._maintain, daemon=True)
            thread.start()
            time.sleep(0.2)
            alive = thread.is_alive()
            self.lib._stop.set()
            thread.join(2)
        self.assertTrue(alive)
        self.assertGreater(len(calls), 1)

    def test_a_full_disk_while_saving_the_mirrored_list_is_not_fatal(self) -> None:
        from unittest import mock
        from falconsdr.playback import index
        self.lib._dirty = True
        with mock.patch.object(index, "atomic_write_text", side_effect=OSError(28, "No space left on device")):
            self.lib.save()


class RecorderTest(unittest.TestCase):
    def _recorder(self):
        from falconsdr.config import RecordingConfig
        from falconsdr.recorder import Recorder
        root = Path(tempfile.mkdtemp(dir=_HOME))
        rec = Recorder(RecordingConfig(enabled=True, output_dir=str(root)), 12500)
        rec.start_session()
        rec.begin_transmission(131_000_000)
        return rec

    def test_trailing_silence_is_dropped_but_a_mid_gap_is_kept(self) -> None:
        import numpy as np
        rec = self._recorder()
        tone, quiet = np.full(100, 0.5, dtype=np.float32), np.zeros(100, dtype=np.float32)
        rec.write_audio(tone)
        rec.write_audio(quiet, real=False)
        rec.write_audio(tone)
        rec.write_audio(quiet, real=False)
        path = rec.end_transmission()
        import wave
        with wave.open(str(path)) as w:
            self.assertEqual(w.getnframes(), 300)

    def test_abort_after_a_write_error_deletes_the_file_and_later_writes_are_noops(self) -> None:
        import numpy as np
        rec = self._recorder()
        path = Path(rec.current_path)
        rec.write_audio(np.full(100, 0.5, dtype=np.float32))
        rec.abort()
        self.assertFalse(path.exists())
        rec.write_audio(np.full(100, 0.5, dtype=np.float32))  # must not raise
        self.assertIsNone(rec.end_transmission())

    def test_a_failing_header_patch_on_close_does_not_raise(self) -> None:
        import numpy as np
        rec = self._recorder()
        rec.write_audio(np.full(100, 0.5, dtype=np.float32))

        class Broken:
            def close(self) -> None:
                raise OSError(28, "No space left on device")
        rec._wav = Broken()
        rec.end_transmission()  # a full disk is not an exception for the caller


if __name__ == "__main__":
    unittest.main()
