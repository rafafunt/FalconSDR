"""APRS: 1200 baud Bell 202 AFSK (1200/2200 Hz) carrying AX.25 UI frames, from the 12.5 kHz audio of an
NFM slot (144.800 MHz in Europe). Written from the public AX.25 and APRS 1.0 descriptions."""
from __future__ import annotations

import re

import numpy as np

from .tone import BitClock, ToneDiscriminator

AUDIO_RATE = 12500
BAUD = 1200
MAX_FRAME_BITS = 8 * 400


def fcs16(data: bytes) -> int:
    """CRC-16/X.25 (reflected 0x8408, init and xorout 0xFFFF): the frame check sequence of AX.25."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


class HdlcDecoder:
    """Bits (after NRZI) -> frames: flags, bit un-stuffing and the frame check."""

    def __init__(self) -> None:
        self.buf: list[int] = []
        self.ones = 0
        self.synced = False
        self.crc_errors = 0

    def push(self, bit: int) -> bytes | None:
        if bit:
            self.ones += 1
            if self.ones >= 7:            # abort: the line went idle or noisy
                self.synced, self.buf = False, []
                return None
            self.buf.append(1)
            return None
        ones, self.ones = self.ones, 0
        self.buf.append(0)
        if ones != 6:
            if len(self.buf) > MAX_FRAME_BITS:
                self.synced, self.buf = False, []
            return None
        frame_bits = self.buf[:-8]        # the flag 01111110 is the last 8 bits
        self.buf = []
        was_synced, self.synced = self.synced, True
        if not was_synced or len(frame_bits) < 8 * 17:
            return None
        data = self._unstuff(frame_bits)
        if data is None or len(data) < 17 or fcs16(data[:-2]) != data[-2] | (data[-1] << 8):
            self.crc_errors += 1
            return None
        return data[:-2]

    @staticmethod
    def _unstuff(bits: list[int]) -> bytes | None:
        out: list[int] = []
        ones = 0
        for b in bits:
            if b:
                ones += 1
                out.append(1)
            else:
                if ones != 5:                 # a zero after five ones is stuffing: dropped
                    out.append(0)
                ones = 0
        if len(out) % 8:
            return None
        return bytes(sum(out[i + k] << k for k in range(8)) for i in range(0, len(out), 8))


def parse_ax25(frame: bytes) -> dict | None:
    """Addresses and the information field of a UI frame."""
    addrs: list[str] = []
    i = 0
    while i + 7 <= len(frame):
        raw = frame[i:i + 7]
        call = bytes(b >> 1 for b in raw[:6]).decode("ascii", "replace").strip()
        ssid = (raw[6] >> 1) & 0x0F
        addrs.append(call + (f"-{ssid}" if ssid else "") + ("*" if raw[6] & 0x80 and len(addrs) >= 2 else ""))
        i += 7
        if raw[6] & 1:
            break
    else:
        return None
    if len(addrs) < 2 or i + 2 > len(frame) or frame[i] & 0xEF != 0x03:
        return None
    return {"source": addrs[1], "dest": addrs[0], "path": addrs[2:], "pid": frame[i + 1], "info": frame[i + 2:]}


# -- APRS information field ------------------------------------------------------------------------

def _lat(text: str) -> float | None:
    m = re.fullmatch(r"(\d{2})([\d ]{2}\.[\d ]{2})([NS])", text)
    if not m:
        return None
    v = int(m.group(1)) + float(m.group(2).replace(" ", "0")) / 60
    return -v if m.group(3) == "S" else v


def _lon(text: str) -> float | None:
    m = re.fullmatch(r"(\d{3})([\d ]{2}\.[\d ]{2})([EW])", text)
    if not m:
        return None
    v = int(m.group(1)) + float(m.group(2).replace(" ", "0")) / 60
    return -v if m.group(3) == "W" else v


def _base91(s: str) -> int:
    v = 0
    for ch in s:
        v = v * 91 + (ord(ch) - 33)
    return v


def _extras(comment: str, out: dict) -> None:
    """Course/speed, altitude and weather found in a comment."""
    m = re.match(r"(\d{3})/(\d{3})", comment)
    if m and "course" not in out:
        out["course"], out["speed_kn"] = int(m.group(1)), int(m.group(2))
        comment = comment[7:]
    alt = re.search(r"/A=(-?\d{6})", comment)
    if alt:
        out["altitude_m"] = round(int(alt.group(1)) * 0.3048)
    out["comment"] = comment.strip()


def _weather(text: str) -> dict:
    out: dict = {}
    for key, name, scale in (("c", "wind_dir", 1), ("s", "wind_mph", 1), ("g", "gust_mph", 1), ("t", "temp_f", 1),
                             ("r", "rain_1h_hin", 1), ("p", "rain_24h_hin", 1), ("P", "rain_day_hin", 1),
                             ("h", "humidity", 1), ("b", "pressure_dpa", 1)):
        m = re.search(key + r"(-?\d{2,5})", text)
        if m:
            out[name] = int(m.group(1)) * scale
    if "pressure_dpa" in out:
        out["pressure_hpa"] = out.pop("pressure_dpa") / 10
    if "temp_f" in out:
        out["temp_c"] = round((out["temp_f"] - 32) * 5 / 9, 1)
    return out


def _position(text: str) -> dict | None:
    """A position (plain or compressed), the symbol, and what follows it."""
    if len(text) >= 19 and text[0].isdigit():
        lat, lon = _lat(text[:8]), _lon(text[9:18])
        if lat is None or lon is None:
            return None
        out = {"lat": round(lat, 5), "lon": round(lon, 5), "symbol": text[8] + text[18]}
        _extras(text[19:], out)
        return out
    if len(text) >= 13 and (text[0] in "/\\" or text[0].isalpha()):
        lat = 90 - _base91(text[1:5]) / 380926
        lon = -180 + _base91(text[5:9]) / 190463
        out = {"lat": round(lat, 5), "lon": round(lon, 5), "symbol": text[0] + text[9]}
        _extras(text[13:], out)
        return out
    return None


def _mic_e(dest: str, info: bytes) -> dict | None:
    if len(info) < 9 or len(dest) < 6:
        return None
    digits, north, lon_offset, west = [], False, False, False
    for i, ch in enumerate(dest[:6]):
        o = ord(ch)
        if 0x30 <= o <= 0x39:
            d, flag = o - 0x30, False
        elif 0x41 <= o <= 0x4A:
            d, flag = o - 0x41, False
        elif 0x50 <= o <= 0x59:
            d, flag = o - 0x50, True
        elif ch in "KLZ":
            d, flag = 0, ch == "Z"
        else:
            return None
        digits.append(d)
        if i == 3:
            north = flag
        elif i == 4:
            lon_offset = flag
        elif i == 5:
            west = flag
    lat = digits[0] * 10 + digits[1] + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100) / 60
    if not north:
        lat = -lat
    deg = info[1] - 28 + (100 if lon_offset else 0)
    if 180 <= deg <= 189:
        deg -= 80
    elif 190 <= deg <= 199:
        deg -= 190
    minutes = info[2] - 28
    if minutes >= 60:
        minutes -= 60
    lon = deg + (minutes + (info[3] - 28) / 100) / 60
    if west:
        lon = -lon
    speed = (info[4] - 28) * 10 + (info[5] - 28) // 10
    course = ((info[5] - 28) % 10) * 100 + (info[6] - 28)
    if speed >= 800:
        speed -= 800
    if course >= 400:
        course -= 400
    out = {"lat": round(lat, 5), "lon": round(lon, 5), "speed_kn": speed, "course": course,
           "symbol": chr(info[8]) + chr(info[7])}
    comment = info[9:].decode("latin-1", "replace").strip()
    if comment:
        out["comment"] = comment
    return out


def parse_aprs(dest: str, info: bytes) -> dict:
    """What an APRS information field says; always has "type" and, when known, "lat"/"lon"."""
    if not info:
        return {"type": "empty"}
    kind, rest = chr(info[0]), info[1:].decode("latin-1")
    if kind in "`'\x1c\x1d":
        pos = _mic_e(dest, info)
        return {"type": "position (Mic-E)", **pos} if pos else {"type": "Mic-E", "text": rest}
    if kind in "!=":
        pos = _position(rest)
    elif kind in "/@":
        pos = _position(rest[7:])
    elif kind == ";":
        pos = _position(rest[17:]) if len(rest) > 17 else None
        if pos:
            return {"type": "object", "name": rest[:9].strip(), "alive": rest[9:10] == "*", **pos}
    elif kind == ":":
        m = re.fullmatch(r"(.{9}):(.*?)(?:\{(\w{1,5}))?", rest, re.S)
        if m:
            out = {"type": "message", "to": m.group(1).strip(), "text": m.group(2)}
            if m.group(3):
                out["msg_no"] = m.group(3)
            return out
        return {"type": "message", "text": rest}
    elif kind == ">":
        return {"type": "status", "text": rest}
    elif kind == "_":
        return {"type": "weather", **_weather(rest)}
    else:
        return {"type": "other", "text": rest}
    if pos is None:
        return {"type": "position", "text": rest}
    out = {"type": "position", **pos}
    if pos.get("symbol", "")[-1:] == "_":                    # a weather station: wind comes as "ddd/sss"
        weather = _weather(pos.get("comment", ""))
        if "course" in out:
            weather["wind_dir"], weather["wind_mph"] = out.pop("course"), out.pop("speed_kn")
        out["weather"] = weather
    return out


class AprsDecoder:
    """Feed it the demodulated NFM audio of a slot (12.5 kHz); ``feed`` returns decoded packets."""

    kind = "aprs"

    def __init__(self) -> None:
        self._disc = ToneDiscriminator(AUDIO_RATE, 1700.0, 1100.0, smooth=3)
        self._clock = BitClock(AUDIO_RATE, BAUD)
        self._hdlc = HdlcDecoder()
        self._last = 0

    def reset(self) -> None:
        self._disc.reset()
        self._clock.reset()
        self._hdlc = HdlcDecoder()
        self._last = 0

    @property
    def crc_errors(self) -> int:
        return self._hdlc.crc_errors

    def feed(self, audio: np.ndarray) -> list[dict]:
        freq = self._disc.process(audio)
        tones = self._clock.process((freq > 0).astype(np.int8))     # 1 = 2200 Hz (space)
        events: list[dict] = []
        for tone in tones:
            bit = 1 if tone == self._last else 0                      # NRZI: no change = 1
            self._last = tone
            frame = self._hdlc.push(bit)
            if frame is None:
                continue
            ax = parse_ax25(frame)
            if ax is None:
                continue
            info = ax.pop("info")
            event = {"kind": "aprs", **ax, "raw": info.decode("latin-1", "replace")}
            event.update(parse_aprs(ax["dest"], info))
            events.append(event)
        return events
