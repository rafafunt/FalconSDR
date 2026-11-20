"""ACARS (VHF, 2400 bit/s MSK, AM voice channel) decoder for the audio of a scanner slot.

Written from the public description of the format (ARINC 618 framing as documented by the open
implementations): pre-key of ones, "+*", SYN SYN, SOH, 7-bit characters with odd parity sent least
significant bit first, a block check sequence (CRC-16) and DEL. Nothing here is taken from another
decoder's source. The mapping of tones to bits is tried in four ways at once (direct, inverted and the
same after differential decoding) and the one whose preamble matches wins, so the decoder does not
depend on a polarity assumption."""
from __future__ import annotations

import numpy as np

from .tone import BitClock, ToneDiscriminator

AUDIO_RATE = 12500
BAUD = 2400
SOH, STX, ETX, ETB, SYN, DEL = 0x01, 0x02, 0x03, 0x17, 0x16, 0x7F
MAX_TEXT = 260            # a block holds up to 220 characters of text; the rest is header and suffix
_HYPOTHESES = ("direct", "inverted", "diff", "diff_inverted")


def with_parity(char: int) -> int:
    """7-bit character + odd parity bit in bit 7."""
    ones = bin(char & 0x7F).count("1")
    return (char & 0x7F) | (0x80 if ones % 2 == 0 else 0)


def char_bits(byte: int) -> list[int]:
    """Bits of one byte in transmission order (least significant first)."""
    return [(byte >> i) & 1 for i in range(8)]


def crc16(data: bytes, crc: int = 0) -> int:
    """CRC-16 with the reflected CCITT polynomial 0x8408 (the block check sequence of ACARS)."""
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


_PREAMBLE = [b for c in (0x2B, 0x2A, SYN, SYN, SOH) for b in char_bits(with_parity(c))]   # "+*", SYN, SYN, SOH
_PREAMBLE_INT = int("".join(map(str, _PREAMBLE)), 2)
_PREAMBLE_MASK = (1 << len(_PREAMBLE)) - 1
_MAX_PREAMBLE_ERRORS = 2


class _Frame:
    """One hypothesis' state while it reads a block after a matching preamble."""

    def __init__(self) -> None:
        self.bits: list[int] = []
        self.chars: list[int] = []     # whole bytes, parity bit included
        self.suffix_at: int | None = None

    def push(self, bit: int):
        """Returns the finished block bytes (without the BCS) and its BCS, 'bad' or None."""
        self.bits.append(bit)
        if len(self.bits) < 8:
            return None
        byte = sum(b << i for i, b in enumerate(self.bits))
        self.bits = []
        if self.suffix_at is None:
            if bin(byte).count("1") % 2 == 0:
                return "bad"                      # parity error: not a block (or wrong tone mapping)
            self.chars.append(byte)
            if (byte & 0x7F) in (ETX, ETB) or len(self.chars) > MAX_TEXT:
                self.suffix_at = len(self.chars)
                if len(self.chars) > MAX_TEXT:
                    return "bad"
            return None
        self.chars.append(byte)                   # the two BCS bytes
        if len(self.chars) == self.suffix_at + 2:
            return bytes(self.chars[:self.suffix_at]), self.chars[self.suffix_at] | (self.chars[self.suffix_at + 1] << 8)
        return None


def parse_block(chars: bytes) -> dict:
    """Fields of a block: ``chars`` are the characters between SOH and the suffix (parity bit included)."""
    text7 = bytes(c & 0x7F for c in chars)
    out: dict = {"mode": chr(text7[0]) if text7 else ""}
    if len(text7) < 12:
        return out
    out["registration"] = text7[1:8].decode("ascii", "replace").lstrip(".")
    ack = text7[8]
    out["ack"] = "NAK" if ack == 0x15 else chr(ack)
    label = text7[9:11]
    out["label"] = "_?" if label == b"_\x7f" else label.decode("ascii", "replace")
    out["block_id"] = chr(text7[11])
    body = text7[12:]
    if body[:1] == bytes([STX]):
        body = body[1:]
        if body[-1:] in (bytes([ETX]), bytes([ETB])):
            out["more"] = body[-1] == ETB
            body = body[:-1]
        if out["block_id"].isdigit() and len(body) >= 10 and out["mode"] not in ("",):  # downlink: message number + flight id
            out["msg_no"] = body[:4].decode("ascii", "replace")
            out["flight"] = body[4:10].decode("ascii", "replace").strip()
            body = body[10:]
        out["text"] = body.decode("ascii", "replace").replace("\r", "").strip("\n")
    return out


class AcarsDecoder:
    """Feed it the demodulated AM audio of a slot (12.5 kHz); ``feed`` returns the decoded blocks."""

    kind = "acars"

    def __init__(self, check_crc: bool = True):
        self._disc = ToneDiscriminator(AUDIO_RATE, 1800.0, 1300.0)
        self._clock = BitClock(AUDIO_RATE, BAUD)
        self._check_crc = check_crc
        self._hist = {h: 0 for h in _HYPOTHESES}
        self._frames: dict[str, _Frame | None] = {h: None for h in _HYPOTHESES}
        self._last_tone = 0
        self._count = 0

    def reset(self) -> None:
        self._disc.reset()
        self._clock.reset()
        self._hist = {h: 0 for h in _HYPOTHESES}
        self._frames = {h: None for h in _HYPOTHESES}
        self._last_tone = 0
        self._count = 0

    def feed(self, audio: np.ndarray) -> list[dict]:
        freq = self._disc.process(audio)
        tones = self._clock.process((freq > 0).astype(np.int8))   # 1 = the higher (2400 Hz) tone
        events: list[dict] = []
        for tone in tones:
            self._count += 1
            bits = {"direct": tone, "inverted": 1 - tone,
                    "diff": 1 if tone == self._last_tone else 0,
                    "diff_inverted": 0 if tone == self._last_tone else 1}
            self._last_tone = tone
            for name, bit in bits.items():
                self._hist[name] = ((self._hist[name] << 1) | bit) & _PREAMBLE_MASK
                frame = self._frames[name]
                if frame is not None:
                    done = frame.push(bit)
                    if done == "bad":
                        self._frames[name] = None
                    elif done is not None:
                        self._frames[name] = None
                        event = self._finish(name, *done)
                        if event is not None:
                            events.append(event)
                            self._hist = {h: 0 for h in _HYPOTHESES}
                            self._frames = {h: None for h in _HYPOTHESES}
                            break
                    continue
                if bin(self._hist[name] ^ _PREAMBLE_INT).count("1") <= _MAX_PREAMBLE_ERRORS:
                    self._frames[name] = _Frame()
        return events

    def _finish(self, hypothesis: str, chars: bytes, bcs: int) -> dict | None:
        # The check covers the characters from after SOH to the suffix (parity bits included).
        ok = crc16(chars + bytes([bcs & 0xFF, bcs >> 8])) == 0
        if self._check_crc and not ok:
            return None
        fields = parse_block(chars)
        fields.update(kind="acars", crc_ok=ok, mapping=hypothesis)
        return fields
