"""Data models shared across FalconSDR (scanner states, channels, blacklist entries)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json


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
    DMR = "DMR"  # digital - digital_voice.py, needs the optional dsd-fme add-on
    YSF = "YSF"  # Yaesu System Fusion (C4FM)


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
    # Optional per-channel overrides (None = follow band/global settings).
    bandwidth_hz: int | None = None
    trigger_level_dbfs: float | None = None
    squelch_enabled: bool | None = None
    squelch_offset_db: float | None = None
    linger_time_ms: int | None = None
    noise_reduction_enabled: bool | None = None
    noise_reduction_strength: float | None = None

    _OVERRIDE_KEYS = (
        "bandwidth_hz", "trigger_level_dbfs", "squelch_enabled", "squelch_offset_db",
        "linger_time_ms", "noise_reduction_enabled", "noise_reduction_strength",
    )

    @property
    def has_overrides(self) -> bool:
        return any(getattr(self, k) is not None for k in self._OVERRIDE_KEYS)

    def to_row(self) -> list[str]:
        ov = {k: getattr(self, k) for k in self._OVERRIDE_KEYS if getattr(self, k) is not None}
        return [
            str(self.freq_hz), self.name, self.modulation.value, "1" if self.enabled else "0", self.folder,
            repr(self.weight), "1" if self.ch833 else "0",
            json.dumps(ov, separators=(",", ":")) if ov else "",
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
        ov: dict = {}
        if len(row) > 7 and row[7].strip():
            try:
                parsed = json.loads(row[7])
                if isinstance(parsed, dict):
                    ov = parsed
            except ValueError:
                pass
        return Channel(
            freq_hz=freq_hz, name=name, modulation=modulation, enabled=enabled, folder=folder, weight=weight, ch833=ch833,
            bandwidth_hz=int(ov["bandwidth_hz"]) if ov.get("bandwidth_hz") is not None else None,
            trigger_level_dbfs=float(ov["trigger_level_dbfs"]) if ov.get("trigger_level_dbfs") is not None else None,
            squelch_enabled=bool(ov["squelch_enabled"]) if ov.get("squelch_enabled") is not None else None,
            squelch_offset_db=float(ov["squelch_offset_db"]) if ov.get("squelch_offset_db") is not None else None,
            linger_time_ms=int(ov["linger_time_ms"]) if ov.get("linger_time_ms") is not None else None,
            noise_reduction_enabled=bool(ov["noise_reduction_enabled"]) if ov.get("noise_reduction_enabled") is not None else None,
            noise_reduction_strength=float(ov["noise_reduction_strength"]) if ov.get("noise_reduction_strength") is not None else None,
        )


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
