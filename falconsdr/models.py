"""Data models shared across FalconSDR (scanner states, channels, blacklist entries)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ScannerState(str, Enum):
    IDLE = "IDLE"
    CALCULATE_NEXT_FREQ = "CALCULATE_NEXT_FREQ"
    TUNING = "TUNING"
    SCANNING = "SCANNING"
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    LINGER = "LINGER"
    RECONNECTING = "RECONNECTING"


class Modulation(str, Enum):
    AM = "AM"
    NFM = "NFM"
    WFM = "WFM"
    SSB = "SSB"  # user-facing only - resolve_ssb_sideband() turns this into USB/LSB
    USB = "USB"
    LSB = "LSB"


# Amateur convention: LSB below 10 MHz, USB above - except 60 m, which is USB.
_SSB_60M_USB_RANGE_HZ = (5_330_500, 5_403_500)


def resolve_ssb_sideband(modulation: Modulation, freq_hz: float) -> Modulation:
    """Modulation.SSB -> USB or LSB for this specific frequency (anything
    else is returned unchanged). Called once wherever a Modulation gets
    attached to a concrete frequency (see scanner_engine.py's _claim()) -
    never stored back into config/channels.csv, so a saved "SSB" channel/band
    keeps re-resolving itself if the user later moves its frequency."""
    if modulation != Modulation.SSB:
        return modulation
    lo, hi = _SSB_60M_USB_RANGE_HZ
    if lo <= freq_hz <= hi:
        return Modulation.USB
    return Modulation.USB if freq_hz >= 10_000_000 else Modulation.LSB


# Audio-path states in which the recorder/speaker are allowed to be live.


@dataclass
class Channel:
    freq_hz: int
    name: str = ""
    modulation: Modulation = Modulation.AM
    enabled: bool = True
    folder: str = ""  # Memory List folder this channel is filed under; "" = top level (no folder)
    # Scan priority weight - 0 (default) means "scan in the usual round-robin
    # order, no special treatment". Anything >0 makes the scanner visit this
    # frequency proportionally more often than other items of the same kind,
    # relative to their own weights (see scanner_engine._WeightedScheduler).
    weight: float = 0.0
    # Airband carrier on the 25 kHz grid published as an 8.33 channel
    # (126.800 -> "126.805") rather than as the 25 kHz channel ("130.875").
    ch833: bool = False

    def to_row(self) -> list[str]:
        return [
            str(self.freq_hz), self.name, self.modulation.value, "1" if self.enabled else "0", self.folder,
            repr(self.weight), "1" if self.ch833 else "0",
        ]

    @staticmethod
    def from_row(row: list[str]) -> "Channel":
        freq_hz = int(float(row[0]))
        name = row[1] if len(row) > 1 else ""
        modulation = Modulation(row[2]) if len(row) > 2 and row[2] in Modulation._value2member_map_ else Modulation.AM
        enabled = row[3] != "0" if len(row) > 3 else True
        folder = row[4] if len(row) > 4 else ""
        try:
            weight = float(row[5]) if len(row) > 5 else 0.0
        except ValueError:
            weight = 0.0
        ch833 = len(row) > 6 and row[6] == "1"
        return Channel(freq_hz=freq_hz, name=name, modulation=modulation, enabled=enabled, folder=folder, weight=weight, ch833=ch833)


@dataclass
class BlacklistEntry:
    freq_hz: int
    margin_hz: int = 5000
    label: str = ""
    date_added: str = ""

    def to_row(self) -> list[str]:
        return [str(self.freq_hz), str(self.margin_hz), self.label, self.date_added]

    @staticmethod
    def from_row(row: list[str]) -> "BlacklistEntry":
        freq_hz = int(float(row[0]))
        margin_hz = int(float(row[1])) if len(row) > 1 else 5000
        label = row[2] if len(row) > 2 else ""
        date_added = row[3] if len(row) > 3 else ""
        return BlacklistEntry(freq_hz=freq_hz, margin_hz=margin_hz, label=label, date_added=date_added)

    def contains(self, freq_hz: int) -> bool:
        return abs(freq_hz - self.freq_hz) <= self.margin_hz


_GUARD_HZ = 121_500_000


def snap_airband_hz(hz: float) -> int:
    """An airband frequency within 1.5 kHz of the 8.33 raster -> that exact
    carrier, so 130.874/130.875 or 134.474 are one channel, not two. Points are
    8.33 kHz apart, so this never merges two real channels. Mirrors carrierHz()."""
    if not 118e6 <= hz <= 137e6 or abs(hz - _GUARD_HZ) < 1500:
        return int(round(hz))
    block = (int(hz) // 25000) * 25000
    snapped = block + round((hz - block) / (25000 / 3)) * 25000 / 3
    return int(round(snapped)) if abs(snapped - hz) <= 1500 else int(round(hz))


def airband_channel(hz: float, ch833: bool = False) -> str:
    """EU airband voice is 8.33 kHz-channelled: pilots dial a channel *name*
    (carrier 126.8000 = "126.805", 126.8083 = "126.810", 126.8167 =
    "126.815"). A 25 kHz-grid carrier keeps its 25 kHz name ("130.875")
    unless ch833; the 121.500 guard stays 25 kHz. "" outside the airband or
    off the raster. Mirrors airbandChannel() in web/static/js/core.js."""
    if not 118e6 <= hz <= 137e6 or abs(hz - _GUARD_HZ) < 500:
        return ""
    block = (int(hz) // 25000) * 25000
    k = round((hz - block) / (25000 / 3))
    if k > 2 or abs(hz - block - k * 25000 / 3) > 500:
        return ""
    if k == 0 and not ch833:
        return f"{block / 1e6:.3f}"
    return f"{(block + (5000, 10000, 15000)[k]) / 1e6:.3f}"
