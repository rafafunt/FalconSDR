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

    def test_csrf_and_dns_rebinding_are_refused(self) -> None:
        url = self._serve("local", "")
        self.assertEqual(self._status(url + "/api/scanner/stop", "POST"), 403)
        self.assertEqual(self._status(url + "/api/scanner/stop", "POST", {"X-FalconSDR": "1"}), 200)
        self.assertEqual(self._status(url + "/api/live", headers={"Host": "evil.example:80"}), 403)


if __name__ == "__main__":
    unittest.main()
