"""Run: python -m unittest tests/test_addons.py

The built-in add-on decoders (APRS, ACARS) against signals made here from the public format descriptions,
the event hub and the external-program runner."""
from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

# Every path the app derives from ~ must land in a scratch home (same as test_core.py).
_HOME = tempfile.mkdtemp(prefix="falconsdr-addon-test-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

RATE = 12500


def _fm(freqs: np.ndarray, amp: float = 0.5) -> np.ndarray:
    """Continuous-phase tone sequence: one frequency (Hz) per sample."""
    return (amp * np.sin(2 * np.pi * np.cumsum(freqs) / RATE)).astype(np.float32)


def _per_symbol(values: list[float], baud: float) -> np.ndarray:
    n = int(len(values) * RATE / baud)
    idx = np.minimum((np.arange(n) * baud / RATE).astype(int), len(values) - 1)
    return np.asarray(values, dtype=float)[idx]


# -- APRS --------------------------------------------------------------------------------------------

def _ax25(src: str, dest: str, path: list[str], info: bytes) -> bytes:
    def addr(call: str, last: bool) -> bytes:
        name, _, ssid = call.partition("-")
        return bytes((ord(c) << 1) for c in name.ljust(6)) + bytes([0x60 | (int(ssid or 0) << 1) | (1 if last else 0)])
    calls = [dest, src] + path
    body = b"".join(addr(c, i == len(calls) - 1) for i, c in enumerate(calls)) + b"\x03\xf0" + info
    from falconsdr.addons.aprs import fcs16
    crc = fcs16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def _afsk(frame: bytes, lead_flags: int = 40, tail_flags: int = 6) -> np.ndarray:
    bits: list[int] = []
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    data = [(byte >> i) & 1 for byte in frame for i in range(8)]
    stuffed, ones = [], 0
    for b in data:
        stuffed.append(b)
        ones = ones + 1 if b else 0
        if ones == 5:
            stuffed.append(0)
            ones = 0
    bits = flag * lead_flags + stuffed + flag * tail_flags
    tone, nrzi = 1, []
    for b in bits:                       # NRZI: a 0 changes the tone
        if b == 0:
            tone ^= 1
        nrzi.append(tone)
    freqs = _per_symbol([2200.0 if t else 1200.0 for t in nrzi], 1200)
    return np.concatenate([np.zeros(500, np.float32), _fm(freqs), np.zeros(500, np.float32)])


def _decode_aprs(audio: np.ndarray) -> list[dict]:
    from falconsdr.addons.aprs import AprsDecoder
    dec = AprsDecoder()
    out: list[dict] = []
    for i in range(0, audio.size, 375):          # the engine's 30 ms blocks
        out += dec.feed(audio[i:i + 375])
    return out


class AprsTest(unittest.TestCase):
    def test_a_position_packet_survives_the_whole_chain(self) -> None:
        info = b"!4903.50N/07201.75W-Test 001234"
        ev = _decode_aprs(_afsk(_ax25("SP9ABC-9", "APRS", ["WIDE1-1", "WIDE2-1"], info)))
        self.assertEqual(len(ev), 1, ev)
        e = ev[0]
        self.assertEqual((e["source"], e["dest"], e["path"]), ("SP9ABC-9", "APRS", ["WIDE1-1", "WIDE2-1"]))
        self.assertAlmostEqual(e["lat"], 49.05833, places=4)
        self.assertAlmostEqual(e["lon"], -72.02917, places=4)
        self.assertEqual((e["symbol"], e["comment"]), ("/-", "Test 001234"))

    def test_packets_with_stuffed_bits_and_in_a_row_are_found_and_noise_is_survived(self) -> None:
        rng = np.random.default_rng(1)
        frames = [_ax25("SP9AAA", "APRS", [], b">status \xff\xff\xff\xfe text"), _ax25("SP9BBB-7", "APRS", [], b"=5009.00N/02157.00E>Mobile")]
        audio = np.concatenate([_afsk(f) for f in frames])
        audio = audio + rng.normal(0, 0.05, audio.size).astype(np.float32)
        ev = _decode_aprs(audio)
        self.assertEqual([e["source"] for e in ev], ["SP9AAA", "SP9BBB-7"])
        self.assertEqual(ev[0]["type"], "status")

    def test_a_damaged_frame_is_dropped_by_the_check(self) -> None:
        frame = bytearray(_ax25("SP9ABC", "APRS", [], b"!4903.50N/07201.75W-x"))
        frame[12] ^= 0x10                                        # one bit flipped inside the frame
        self.assertEqual(_decode_aprs(_afsk(bytes(frame))), [])

    def test_the_information_field_parser(self) -> None:
        from falconsdr.addons.aprs import parse_aprs
        c = parse_aprs("APRS", b"=/5L!!<*e7>7P[")                  # compressed position (the APRS 1.0 example)
        self.assertAlmostEqual(c["lat"], 49.5, places=3)
        self.assertAlmostEqual(c["lon"], -72.75, places=3)
        m = parse_aprs("APRS", b":SP9ABC-9 :Hello there{42")
        self.assertEqual((m["type"], m["to"], m["text"], m["msg_no"]), ("message", "SP9ABC-9", "Hello there", "42"))
        w = parse_aprs("APRS", b"!4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900")
        self.assertEqual((w["weather"]["wind_dir"], w["weather"]["temp_f"], w["weather"]["humidity"]), (220, 77, 50))
        self.assertAlmostEqual(w["weather"]["pressure_hpa"], 990.0)
        o = parse_aprs("APRS", b";LEADER   *092345z4903.50N/07201.75W>088/036")
        self.assertEqual((o["type"], o["name"], o["course"], o["speed_kn"]), ("object", "LEADER", 88, 36))

    def test_mic_e(self) -> None:
        from falconsdr.addons.aprs import parse_aprs
        # Made with the Mic-E rules: 33 deg 25.64' N, 112 deg 07.74' W, 36 kn at 251 deg.
        dest, info = "SUQ5V4", bytes([0x60, 112 - 100 + 28 + 100 - 100 + 0, 7 + 28, 74 + 28, 3 + 28, 2 * 10 + 28 + 5, 1 + 28, ord("j"), ord("/")])
        # lat digits 3 3 2 5 6 4 -> 'S' '3'->'3' ... built below instead of by hand
        digits = [3, 3, 2, 5, 6, 4]
        flags = [False, False, False, True, True, True]               # N, +100 deg lon offset, West
        dest = "".join(chr((0x50 if f else 0x30) + d) for d, f in zip(digits, flags))
        speed, course = 36, 251
        info = bytes([0x60, 112 - 100 + 28, 7 + 28, 74 + 28, (speed // 10) + 28, ((speed % 10) * 10 + course // 100) + 28, (course % 100) + 28, ord("j"), ord("/")])
        p = parse_aprs(dest, info)
        self.assertAlmostEqual(p["lat"], 33 + 25.64 / 60, places=4)
        self.assertAlmostEqual(p["lon"], -(112 + 7.74 / 60), places=4)
        self.assertEqual((p["speed_kn"], p["course"]), (36, 251))


# -- ACARS -------------------------------------------------------------------------------------------

def _acars_block(text: bytes, registration: str = ".SP-LWA", label: str = "H1", block_id: str = "2") -> bytes:
    from falconsdr.addons import acars
    body = (b"2" + registration.encode().ljust(7, b".")[:7] + b"\x15" + label.encode() + block_id.encode()
            + bytes([acars.STX]) + text + bytes([acars.ETX]))
    chars = bytes(acars.with_parity(c) for c in body)
    crc = acars.crc16(chars)
    return chars + bytes([crc & 0xFF, crc >> 8])


def _acars_audio(block: bytes, mapping: str) -> np.ndarray:
    from falconsdr.addons import acars
    head = [b for c in (0x2B, 0x2A, acars.SYN, acars.SYN, acars.SOH) for b in acars.char_bits(acars.with_parity(c))]
    bits = [1] * 16 + head + [b for byte in block for b in acars.char_bits(byte)] + [b for b in acars.char_bits(acars.DEL)]
    tones, tone = [], 1
    for b in bits:
        if mapping == "direct":
            tone = b
        elif mapping == "inverted":
            tone = 1 - b
        elif mapping == "diff":              # no change = 1
            tone = tone if b else 1 - tone
        tones.append(tone)
    freqs = _per_symbol([2400.0 if t else 1200.0 for t in tones], 2400)
    return np.concatenate([np.zeros(300, np.float32), _fm(freqs), np.zeros(300, np.float32)])


def _decode_acars(audio: np.ndarray, **kw) -> list[dict]:
    from falconsdr.addons.acars import AcarsDecoder
    dec = AcarsDecoder(**kw)
    out: list[dict] = []
    for i in range(0, audio.size, 375):
        out += dec.feed(audio[i:i + 375])
    return out


class AcarsTest(unittest.TestCase):
    def test_a_block_is_decoded_whatever_the_tone_mapping(self) -> None:
        block = _acars_block(b"M01AAB1234HELLO FROM THE COCKPIT\r\n")
        for mapping in ("direct", "inverted", "diff"):
            ev = _decode_acars(_acars_audio(block, mapping))
            self.assertEqual(len(ev), 1, mapping)
            e = ev[0]
            self.assertTrue(e["crc_ok"], mapping)
            self.assertEqual((e["registration"], e["label"], e["msg_no"], e["flight"]), ("SP-LWA", "H1", "M01A", "AB1234"), mapping)
            self.assertIn("HELLO FROM THE COCKPIT", e["text"])

    def test_noise_and_a_damaged_block_give_nothing(self) -> None:
        rng = np.random.default_rng(3)
        self.assertEqual(_decode_acars(rng.normal(0, 0.2, RATE * 3).astype(np.float32)), [])
        block = bytearray(_acars_block(b"M01AAB1234TEXT"))
        parity = bytearray(block)
        parity[20] ^= 0x01                                       # a character with a parity error: not a block
        self.assertEqual(_decode_acars(_acars_audio(bytes(parity), "direct"), check_crc=False), [])
        block[-1] ^= 0x01                                        # only the check sequence is wrong
        self.assertEqual(_decode_acars(_acars_audio(bytes(block), "direct")), [])
        shown = _decode_acars(_acars_audio(bytes(block), "direct"), check_crc=False)    # kept when asked (diagnostics)
        self.assertEqual((len(shown), shown[0]["crc_ok"]), (1, False))

    def test_crc16_matches_the_known_check_value(self) -> None:
        from falconsdr.addons.acars import crc16
        self.assertEqual(crc16(b"123456789"), 0x2189)          # CRC-16/KERMIT


# -- hub, manager, external programs, engine ---------------------------------------------------------

class HubTest(unittest.TestCase):
    def test_events_get_ids_survive_a_restart_and_old_days_are_pruned(self) -> None:
        from datetime import date, timedelta
        from pathlib import Path
        from falconsdr.addons.events import EventHub
        folder = Path(tempfile.mkdtemp(dir=_HOME))
        old = folder / f"events-{(date.today() - timedelta(days=30)).isoformat()}.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        hub = EventHub(folder, keep_days=7)
        a = hub.publish("aprs", {"kind": "aprs"}, "one")
        b = hub.publish("acars", {"kind": "acars"}, "two")
        self.assertEqual((a["id"], b["id"]), (1, 2))
        self.assertEqual([e["summary"] for e in hub.since(1)], ["two"])
        self.assertEqual([e["summary"] for e in hub.since(0, addon="aprs")], ["one"])
        self.assertFalse(old.exists())
        again = EventHub(folder, keep_days=7)                     # what is on disk comes back
        self.assertEqual([e["summary"] for e in again.since(0)], ["one", "two"])


class ManagerTest(unittest.TestCase):
    def _manager(self):
        from falconsdr.addons.manager import AddonManager
        from falconsdr.config import AppConfig
        return AddonManager(AppConfig())

    def test_a_channel_is_claimed_only_by_an_enabled_add_on_and_only_the_nearest_grid_point(self) -> None:
        from falconsdr.models import Modulation
        m = self._manager()
        self.assertIsNone(m.claim(131_725_000))                    # off by default
        m.configure("acars", {"enabled": True})
        claim = m.claim(131_724_000)                               # the 8.333 kHz grid is never exactly on it
        self.assertEqual((claim.addon_id, claim.modulation), ("acars", Modulation.AM))
        self.assertIsNone(m.claim(131_716_667))                    # the neighbouring grid point is a voice channel
        m.configure("aprs", {"enabled": True})
        self.assertEqual(m.claim(144_800_000).modulation, Modulation.NFM)    # whatever the band says
        m.configure("acars", {"enabled": False, "frequencies": [131_550_000]})
        self.assertIsNone(m.claim(131_725_000))
        self.assertTrue(m.status()[0]["frequencies"] == [131_550_000])

    def test_external_programs_are_found_or_reported_missing(self) -> None:
        m = self._manager()
        rows = {r["id"]: r for r in m.status()}
        self.assertTrue(rows["acars"]["installed"])
        self.assertEqual(rows["vdl2"]["installed"], bool(rows["vdl2"]["program"]))
        m.configure("vdl2", {"path": "/no/such/dumpvdl2"})
        self.assertFalse({r["id"]: r for r in m.status()}["vdl2"]["installed"])
        with self.assertRaises(RuntimeError):
            m.start_external("vdl2", {"driver": "auto", "device_args": ""})
        import sys
        m.configure("rtl_433", {"path": sys.executable})           # any existing file: only the receiver is checked now
        with self.assertRaises(RuntimeError):
            m.start_external("rtl_433", {"driver": "hackrf", "device_args": ""})

    def test_the_command_line_names_the_dongle_and_the_channels(self) -> None:
        m = self._manager()
        self.assertEqual(m._device_args("vdl2", {"driver": "auto", "device_args": "serial=0001"}), ["--rtlsdr", "0001"])
        self.assertEqual(m._device_args("rtl_433", {"driver": "rtlsdr", "device_args": "serial=0001"}), ["-d", ":0001"])
        self.assertEqual(m._device_args("rtl_433", {"driver": "rtlsdr", "device_args": "index=1"}), ["-d", "1"])
        self.assertEqual(m._device_args("rtl_433", {"driver": "auto", "device_args": ""}), [])


class ExternalTest(unittest.TestCase):
    def test_lines_are_read_and_the_exit_is_reported(self) -> None:
        import sys
        import threading
        from falconsdr.addons.external import ExternalRunner
        code = "import json;print(json.dumps({'model':'Acurite-Tower','id':7,'temperature_C':21.5,'humidity':55}),flush=True);print('noise')"
        lines, done = [], threading.Event()
        runner = ExternalRunner([sys.executable, "-c", code], lines.append, lambda c: (lines.append(f"exit {c}"), done.set()))
        runner.start()
        self.assertTrue(done.wait(10))
        self.assertEqual(lines[-1], "exit 0")
        self.assertEqual(len(lines), 3)

    def test_stop_ends_a_running_program(self) -> None:
        import sys
        import threading
        from falconsdr.addons.external import ExternalRunner
        done = threading.Event()
        runner = ExternalRunner([sys.executable, "-c", "import time;print('x',flush=True);time.sleep(60)"], lambda l: None, lambda c: done.set())
        runner.start()
        self.assertTrue(runner.alive)
        runner.stop()
        self.assertTrue(done.wait(10))
        self.assertFalse(runner.alive)

    def test_what_rtl_433_and_dumpvdl2_print(self) -> None:
        from falconsdr.addons.external import parse_rtl_433, parse_vdl2
        event, summary = parse_rtl_433('{"time":"2026-11-16 10:00:00","model":"Acurite-Tower","id":7,"channel":"A","temperature_C":21.5,"humidity":55,"battery_ok":1}')
        self.assertEqual((event["kind"], event["device_id"]), ("sensor", 7))
        self.assertIn("temperature_C 21.5", summary)
        self.assertIsNone(parse_rtl_433("not json"))
        line = ('{"vdl2":{"freq":136975000,"sig_level":-30.1,"avlc":{"src":{"addr":"48AF12","type":"Aircraft"},"dst":{"addr":"2B1234","type":"Ground station"},'
                '"frame_type":"I","acars":{"reg":".SP-LWA","label":"H1","flight":"LO123","msg_text":"POS N5000.1E02100.2"}}}}')
        event, summary = parse_vdl2(line)
        self.assertEqual((event["kind"], event["registration"], event["freq_hz"]), ("vdl2", "SP-LWA", 136975000))
        self.assertIn("LO123", summary)
        event, summary = parse_vdl2('{"vdl2":{"freq":136725000,"avlc":{"src":{"addr":"AAAAAA"},"dst":{"addr":"BBBBBB"},"frame_type":"S"}}}')
        self.assertEqual(summary, "AAAAAA → BBBBBB S")


class EngineSlotTest(unittest.TestCase):
    """A claimed channel is decoded, not played or recorded - and a voice channel keeps the speaker."""
    def _setup(self, listen: bool):
        from types import SimpleNamespace
        from unittest import mock
        from falconsdr.addons.aprs import AprsDecoder
        from falconsdr.addons.manager import AddonManager
        from falconsdr.config import AppConfig
        from falconsdr.models import Modulation
        from falconsdr.scanner_engine import ScannerEngine
        cfg = AppConfig()
        cfg.addons.enabled, cfg.addons.listen_to_data = ["aprs"], listen
        mgr = AddonManager(cfg)
        audio = _afsk(_ax25("SP9ABC-9", "APRS", ["WIDE1-1"], b"!4903.50N/07201.75W-engine"))
        blocks = iter([audio[i:i + 375] for i in range(0, audio.size, 375)])
        slot = SimpleNamespace(dsd=None, data_decoder=AprsDecoder(), data_addon="aprs", freq_hz=144_800_000, offset_hz=0.0,
                               modulation=Modulation.NFM, demod=SimpleNamespace(demod=lambda iq, mod, off: next(blocks)))
        fake = SimpleNamespace(addons=mgr, _play_and_record_slot=mock.Mock(), signals=SimpleNamespace(log=SimpleNamespace(emit=lambda m: None)))
        fake._is_silent_data = lambda s: ScannerEngine._is_silent_data(fake, s)
        fake._decode_data = lambda s, a: ScannerEngine._decode_data(fake, s, a)
        return ScannerEngine, fake, slot, mgr, audio

    def test_data_channel_is_decoded_and_stays_off_the_speaker(self) -> None:
        engine, fake, slot, mgr, audio = self._setup(listen=False)
        for _ in range(-(-audio.size // 375)):
            engine._process_one_slot(fake, slot, None, False)
        rows = mgr.hub.since(0)
        self.assertEqual([r["source"] for r in rows], ["SP9ABC-9"])
        self.assertEqual((rows[0]["addon"], rows[0]["freq_hz"]), ("aprs", 144_800_000))
        self.assertIn("49.0583", rows[0]["summary"])
        fake._play_and_record_slot.assert_not_called()

    def test_listen_to_data_plays_it_too(self) -> None:
        engine, fake, slot, mgr, audio = self._setup(listen=True)
        engine._process_one_slot(fake, slot, None, False)
        fake._play_and_record_slot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
