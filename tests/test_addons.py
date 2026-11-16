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


if __name__ == "__main__":
    unittest.main()
