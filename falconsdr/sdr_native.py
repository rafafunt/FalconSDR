"""Experimental native backends for HackRF (libhackrf) and Airspy (libairspy), loaded with ctypes.

Both libraries are BSD-3-Clause (headers checked 2026-10-31), so FalconSDR (GPL-3.0-or-later) may use and
even ship them; for now they are only *found*, never bundled: a system package (``libhackrf0``,
``libairspy0``), a DLL next to the program, or ``FALCONSDR_LIBHACKRF`` / ``FALCONSDR_LIBAIRSPY``.
SoapySDR stays the default for these two (it is the better tested path); this is the way out when it is
missing or fails. Neither has been run against real hardware by the author - ``main.py --probe-sdr``
measures what the code can only assume (stale samples after a retune, the gain steps, losses).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from .sdr_backend import SdrBackend, SoapyBackend

HACKRF_NATIVE_DRIVER = "hackrf_native"
AIRSPY_NATIVE_DRIVER = "airspy_native"

_HACKRF_SAMPLE_RATES_HZ = (2_000_000.0, 20_000_000.0)   # what the board is specified for
_HACKRF_GAIN_RANGE_DB = (0.0, 116.0)                    # LNA 40 + VGA 62 + AMP 14, nominal
_AIRSPY_GAIN_RANGE_DB = (0.0, 21.0)                     # the library's linearity/sensitivity index
_SETTLE_S = 0.003                                       # the PLLs after hackrf_set_freq() returns


def _find_library(names: tuple[str, ...], env: str, find_name: str) -> ctypes.CDLL | None:
    candidates: list[str] = []
    if os.environ.get(env):
        candidates.append(os.environ[env])
    folders = [Path(sys.executable).parent, Path(__file__).resolve().parent.parent]
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        folders.insert(0, Path(sys._MEIPASS))
    for folder in folders:
        candidates += [str(folder / n) for n in names if (folder / n).exists()]
    candidates += list(names)
    found = ctypes.util.find_library(find_name)
    if found:
        candidates.append(found)
    for name in candidates:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------------- the stream
class CallbackStream:
    """The bytes a vendor library hands to its callback, given back to read() in the size it asks for.

    The libraries stream by themselves and queue transfers, so after a retune the next few
    transfers still hold the old frequency (measured on SoapySDR: 3.9 blocks, the whole dwell read as
    the previous window). ``begin_retune`` marks the instant the tuner moved; a transfer counts only
    when all of it was produced after that (it arrives when full, so its first byte is one transfer
    older than its arrival), plus one more as margin for a callback delayed by the GIL. Nothing here
    needs hardware, which is the point: the arithmetic is tested with made-up arrival times.
    """

    STALE_MARGIN_CHUNKS = 1

    def __init__(self, bytes_per_sample: int, sample_rate: float, max_buffer_s: float = 2.0, timeout_s: float = 1.0):
        self.bytes_per_sample = bytes_per_sample
        self.sample_rate = sample_rate
        self._max_bytes = int(max_buffer_s * sample_rate * bytes_per_sample)
        self._timeout_s = timeout_s
        self._cond = threading.Condition()
        self._chunks: deque[bytes] = deque()
        self._buffered = 0
        self._error: str | None = None
        self._drop_until = 0.0
        self.received_samples = 0
        self.lost_samples = 0           # our own overflow plus whatever the library reports
        self.stale_samples = 0          # thrown away after a retune - not a loss

    def push(self, data: bytes, now: float) -> None:
        samples = len(data) // self.bytes_per_sample
        seconds = samples / self.sample_rate
        with self._cond:
            if now - seconds * (1 + self.STALE_MARGIN_CHUNKS) < self._drop_until:
                self.stale_samples += samples
                return
            self.received_samples += samples
            self._chunks.append(data)
            self._buffered += len(data)
            while self._buffered > self._max_bytes and len(self._chunks) > 1:
                old = self._chunks.popleft()
                self._buffered -= len(old)
                self.lost_samples += len(old) // self.bytes_per_sample
            self._cond.notify()

    def add_lost(self, samples: int) -> None:
        with self._cond:
            self.lost_samples += max(0, samples)

    def begin_retune(self, at: float) -> None:
        with self._cond:
            self._drop_until = at + _SETTLE_S
            self._chunks.clear()
            self._buffered = 0

    def fail(self, message: str) -> None:
        with self._cond:
            self._error = message
            self._cond.notify_all()

    def read(self, num_samples: int) -> np.ndarray:
        """Exactly ``num_samples`` as bytes (a uint8 array), or RuntimeError (gotcha 8)."""
        need = num_samples * self.bytes_per_sample
        out = np.empty(need, dtype=np.uint8)
        got = 0
        deadline = time.monotonic() + self._timeout_s
        with self._cond:
            while got < need:
                if self._error:
                    raise RuntimeError(self._error)
                if not self._chunks:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError(
                            f"SDR stream is delivering too little data ({got // self.bytes_per_sample}/{num_samples} "
                            "samples) - device disconnected or not responding")
                    self._cond.wait(remaining)
                    continue
                chunk = self._chunks[0]
                take = min(len(chunk), need - got)
                out[got:got + take] = np.frombuffer(chunk, dtype=np.uint8, count=take)
                got += take
                self._buffered -= take
                if take == len(chunk):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[take:]
        return out

    def stats(self) -> tuple[float, float]:
        """(seconds lost, seconds received) since the stream began."""
        return self.lost_samples / self.sample_rate, self.received_samples / self.sample_rate


class _NativeStreamBackend(SdrBackend):
    """What HackRF and Airspy share: a library that streams into a callback, kept running all the time."""
    streaming = True        # AcquisitionThread: the spacing of reads says nothing about lost samples
    bytes_per_sample = 8

    def __init__(self, sample_rate: float):
        self.sample_rate = sample_rate
        self.bias_tee = False
        self._stream = CallbackStream(self.bytes_per_sample, sample_rate)
        self._cb = None          # the ctypes callback must outlive the stream
        self._closed = False

    def read_raw(self, num_samples: int) -> np.ndarray:
        return self._stream.read(num_samples)

    def read(self, num_samples: int) -> np.ndarray:
        return self.to_iq(self.read_raw(num_samples))

    def stream_stats(self) -> tuple[float, float]:
        return self._stream.stats()

    @property
    def stale_samples(self) -> int:
        return self._stream.stale_samples


# --------------------------------------------------------------------------------------- HackRF
class _HackrfTransfer(ctypes.Structure):
    _fields_ = [("device", ctypes.c_void_p), ("buffer", ctypes.POINTER(ctypes.c_uint8)),
                ("buffer_length", ctypes.c_int), ("valid_length", ctypes.c_int),
                ("rx_ctx", ctypes.c_void_p), ("tx_ctx", ctypes.c_void_p)]


class _HackrfDeviceList(ctypes.Structure):
    _fields_ = [("serial_numbers", ctypes.POINTER(ctypes.c_char_p)), ("usb_board_ids", ctypes.POINTER(ctypes.c_int)),
                ("usb_device_index", ctypes.POINTER(ctypes.c_int)), ("devicecount", ctypes.c_int),
                ("usb_devices", ctypes.POINTER(ctypes.c_void_p)), ("usb_devicecount", ctypes.c_int)]


_HACKRF_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_HackrfTransfer))
_HACKRF_BOARDS = {0x604B: "HackRF Jawbreaker", 0x6089: "HackRF One", 0xCC15: "rad1o"}

_hackrf_lib: ctypes.CDLL | None = None
_hackrf_tried = False
_lib_lock = threading.Lock()


def hackrf_library() -> ctypes.CDLL | None:
    """libhackrf with its prototypes declared and hackrf_init() done (it is left initialised for the life
    of the process: hackrf_exit() with a device open fails and a second init is a no-op), or None."""
    global _hackrf_lib, _hackrf_tried
    with _lib_lock:
        if _hackrf_tried:
            return _hackrf_lib
        _hackrf_tried = True
        lib = _find_library(("libhackrf.so.0", "libhackrf.so", "hackrf.dll", "libhackrf.dll", "libhackrf.dylib"),
                            "FALCONSDR_LIBHACKRF", "hackrf")
        if lib is None:
            return None
        c = ctypes
        try:
            sig = {
                "hackrf_init": ([], c.c_int), "hackrf_exit": ([], c.c_int),
                "hackrf_open": ([c.POINTER(c.c_void_p)], c.c_int),
                "hackrf_open_by_serial": ([c.c_char_p, c.POINTER(c.c_void_p)], c.c_int),
                "hackrf_close": ([c.c_void_p], c.c_int),
                "hackrf_device_list": ([], c.POINTER(_HackrfDeviceList)),
                "hackrf_device_list_free": ([c.POINTER(_HackrfDeviceList)], None),
                "hackrf_start_rx": ([c.c_void_p, _HACKRF_CB, c.c_void_p], c.c_int),
                "hackrf_stop_rx": ([c.c_void_p], c.c_int),
                "hackrf_set_sample_rate": ([c.c_void_p, c.c_double], c.c_int),
                "hackrf_set_freq": ([c.c_void_p, c.c_uint64], c.c_int),
                "hackrf_set_lna_gain": ([c.c_void_p, c.c_uint32], c.c_int),
                "hackrf_set_vga_gain": ([c.c_void_p, c.c_uint32], c.c_int),
                "hackrf_set_amp_enable": ([c.c_void_p, c.c_uint8], c.c_int),
                "hackrf_set_antenna_enable": ([c.c_void_p, c.c_uint8], c.c_int),
                "hackrf_error_name": ([c.c_int], c.c_char_p),
            }
            for name, (args, res) in sig.items():
                fn = getattr(lib, name)
                fn.argtypes, fn.restype = args, res
            if lib.hackrf_init() != 0:
                return None
        except (AttributeError, OSError):
            return None
        _hackrf_lib = lib
        return lib


def _hackrf_error(lib, code: int) -> str:
    try:
        return f"{lib.hackrf_error_name(code).decode()} ({code})"
    except Exception:  # noqa: BLE001
        return str(code)


def hackrf_gain_split(gain_db: float) -> tuple[int, int, bool]:
    """One gain number -> (LNA dB, VGA dB, AMP on). LNA moves in 8 dB steps (0-40), VGA in 2 dB steps
    (0-62); the 14 dB amp only above what those two give together (102 dB). LNA gets the larger share
    (40:62) - the front end's noise figure is set before the VGA. Totals are exact to within 1 dB; the
    library would otherwise round each stage down silently."""
    g = max(0.0, min(_HACKRF_GAIN_RANGE_DB[1], float(gain_db)))
    amp = g > 102.0
    if amp:
        g -= 14.0
    lna = int(min(40, max(0, round(g * 40.0 / 102.0 / 8.0) * 8)))
    vga = int(min(62, max(0, round((g - lna) / 2.0) * 2)))
    return lna, vga, amp


class HackRfBackend(_NativeStreamBackend):
    label = HACKRF_NATIVE_DRIVER
    bytes_per_sample = 2

    def __init__(self, device_args: str, sample_rate: float, gain_mode: str, gain_db: float, bias_tee: bool = False):
        lib = hackrf_library()
        if lib is None:
            raise RuntimeError("libhackrf is not installed (Debian/Ubuntu: sudo apt install libhackrf0)")
        lo, hi = _HACKRF_SAMPLE_RATES_HZ
        if not lo <= sample_rate <= hi:
            raise RuntimeError(f"HackRF captures 2 to 20 MS/s - set the capture width to 2..20 MHz (now {sample_rate / 1e6:.2f})")
        super().__init__(sample_rate)
        self._lib = lib
        self.gain_range_db = _HACKRF_GAIN_RANGE_DB
        SoapyBackend._wait_for_reopen_cooldown()
        args = {k.strip(): v.strip() for k, v in (kv.split("=", 1) for kv in (device_args or "").split(",") if "=" in kv)}
        dev = ctypes.c_void_p()
        serial = args.get("serial")
        rc = lib.hackrf_open_by_serial(serial.encode(), ctypes.byref(dev)) if serial else lib.hackrf_open(ctypes.byref(dev))
        if rc != 0:
            raise RuntimeError(f"Could not open the HackRF: {_hackrf_error(lib, rc)}")
        self._dev = dev
        self._open = True
        try:
            self._check(lib.hackrf_set_sample_rate(dev, sample_rate), "set_sample_rate")   # also sets the baseband filter
            lib.hackrf_set_antenna_enable(dev, 0)      # not every board has it: the answer does not matter when off
            self.set_gain(gain_mode, gain_db)
            self._cb = _HACKRF_CB(self._on_data)
            self._check(lib.hackrf_start_rx(dev, self._cb, None), "start_rx")
            if bias_tee:
                self._check(lib.hackrf_set_antenna_enable(dev, 1), "antenna power")
                self.bias_tee = True
        except Exception:
            self.close()
            raise

    def _check(self, rc: int, what: str) -> None:
        if rc != 0:
            raise RuntimeError(f"HackRF {what} failed: {_hackrf_error(self._lib, rc)}")

    def _on_data(self, transfer_p) -> int:
        try:
            t = transfer_p.contents
            if t.valid_length > 0:
                self._stream.push(ctypes.string_at(t.buffer, t.valid_length), time.monotonic())
        except Exception as exc:  # noqa: BLE001 - never let an exception unwind into C
            self._stream.fail(f"HackRF callback failed: {exc}")
            return 1
        return 0

    def set_frequency(self, freq_hz: float) -> None:
        self._check(self._lib.hackrf_set_freq(self._dev, int(freq_hz)), "set_freq")
        self._stream.begin_retune(time.monotonic())

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        # No hardware AGC on this board: "auto" keeps the last manual setting.
        lna, vga, amp = hackrf_gain_split(gain_db)
        self._check(self._lib.hackrf_set_lna_gain(self._dev, lna), "set_lna_gain")
        self._check(self._lib.hackrf_set_vga_gain(self._dev, vga), "set_vga_gain")
        self._check(self._lib.hackrf_set_amp_enable(self._dev, 1 if amp else 0), "set_amp_enable")
        if getattr(self, "_cb", None) is not None:
            self._stream.begin_retune(time.monotonic())    # a gain step moves the level inside the queued transfers too

    @staticmethod
    def to_iq(raw: np.ndarray) -> np.ndarray:
        """Signed 8-bit I/Q -> CF32, full scale = 128 (the same dBFS reference as the RTL-SDR paths)."""
        return (raw.view(np.int8).astype(np.float32) * np.float32(1 / 128)).view(np.complex64)

    def close(self) -> None:
        if not getattr(self, "_open", False):
            return
        self._open = False
        lib, dev = self._lib, self._dev
        for call in (lambda: lib.hackrf_stop_rx(dev), lambda: lib.hackrf_set_antenna_enable(dev, 0),
                     lambda: lib.hackrf_set_amp_enable(dev, 0)):
            try:
                call()
            except Exception:  # noqa: BLE001
                pass
        lib.hackrf_close(dev)
        SoapyBackend._record_close_time()

    @staticmethod
    def enumerate_devices() -> list[dict]:
        lib = hackrf_library()
        if lib is None:
            return []
        out: list[dict] = []
        try:
            lst = lib.hackrf_device_list()
            if not lst:
                return []
            try:
                for i in range(lst.contents.devicecount):
                    serial = lst.contents.serial_numbers[i]
                    board = _HACKRF_BOARDS.get(lst.contents.usb_board_ids[i], "HackRF")
                    if serial:
                        text = serial.decode()
                        out.append({"driver": HACKRF_NATIVE_DRIVER, "label": f"{board} (libhackrf) SN {text[-8:]}", "serial": text})
                    else:   # busy or unreadable: only the USB order tells it apart
                        out.append({"driver": HACKRF_NATIVE_DRIVER, "label": f"{board} #{i + 1} (libhackrf)", "index": str(i)})
            finally:
                lib.hackrf_device_list_free(lst)
        except Exception:  # noqa: BLE001
            return []
        return out


# --------------------------------------------------------------------------------------- Airspy
class _AirspyTransfer(ctypes.Structure):
    _fields_ = [("device", ctypes.c_void_p), ("ctx", ctypes.c_void_p), ("samples", ctypes.c_void_p),
                ("sample_count", ctypes.c_int), ("dropped_samples", ctypes.c_uint64), ("sample_type", ctypes.c_int)]


_AIRSPY_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_AirspyTransfer))
_AIRSPY_FLOAT32_IQ = 0

_airspy_lib: ctypes.CDLL | None = None
_airspy_tried = False


def airspy_library() -> ctypes.CDLL | None:
    global _airspy_lib, _airspy_tried
    with _lib_lock:
        if _airspy_tried:
            return _airspy_lib
        _airspy_tried = True
        lib = _find_library(("libairspy.so.0", "libairspy.so", "airspy.dll", "libairspy.dll", "libairspy.dylib"),
                            "FALCONSDR_LIBAIRSPY", "airspy")
        if lib is None:
            return None
        c = ctypes
        try:
            sig = {
                "airspy_open": ([c.POINTER(c.c_void_p)], c.c_int),
                "airspy_open_sn": ([c.POINTER(c.c_void_p), c.c_uint64], c.c_int),
                "airspy_close": ([c.c_void_p], c.c_int),
                "airspy_list_devices": ([c.POINTER(c.c_uint64), c.c_int], c.c_int),
                "airspy_get_samplerates": ([c.c_void_p, c.POINTER(c.c_uint32), c.c_uint32], c.c_int),
                "airspy_set_samplerate": ([c.c_void_p, c.c_uint32], c.c_int),
                "airspy_set_sample_type": ([c.c_void_p, c.c_int], c.c_int),
                "airspy_start_rx": ([c.c_void_p, _AIRSPY_CB, c.c_void_p], c.c_int),
                "airspy_stop_rx": ([c.c_void_p], c.c_int),
                "airspy_set_freq": ([c.c_void_p, c.c_uint32], c.c_int),
                "airspy_set_sensitivity_gain": ([c.c_void_p, c.c_uint8], c.c_int),
                "airspy_set_lna_agc": ([c.c_void_p, c.c_uint8], c.c_int),
                "airspy_set_mixer_agc": ([c.c_void_p, c.c_uint8], c.c_int),
                "airspy_set_rf_bias": ([c.c_void_p, c.c_uint8], c.c_int),
                "airspy_error_name": ([c.c_int], c.c_char_p),
            }
            for name, (args, res) in sig.items():
                fn = getattr(lib, name)
                fn.argtypes, fn.restype = args, res
        except (AttributeError, OSError):
            return None
        _airspy_lib = lib
        return lib


def _airspy_error(lib, code: int) -> str:
    try:
        return f"{lib.airspy_error_name(code).decode()} ({code})"
    except Exception:  # noqa: BLE001
        return str(code)


class AirspyBackend(_NativeStreamBackend):
    label = AIRSPY_NATIVE_DRIVER
    bytes_per_sample = 8        # FLOAT32_IQ: already complex64 in the dBFS scale (+-1.0 = full scale)

    def __init__(self, device_args: str, sample_rate: float, gain_mode: str, gain_db: float, bias_tee: bool = False):
        lib = airspy_library()
        if lib is None:
            raise RuntimeError("libairspy is not installed (Debian/Ubuntu: sudo apt install libairspy0)")
        super().__init__(sample_rate)
        self._lib = lib
        self.gain_range_db = _AIRSPY_GAIN_RANGE_DB
        SoapyBackend._wait_for_reopen_cooldown()
        args = {k.strip(): v.strip() for k, v in (kv.split("=", 1) for kv in (device_args or "").split(",") if "=" in kv)}
        dev = ctypes.c_void_p()
        serial = args.get("serial")
        rc = lib.airspy_open_sn(ctypes.byref(dev), int(serial, 16)) if serial else lib.airspy_open(ctypes.byref(dev))
        if rc != 0:
            raise RuntimeError(f"Could not open the Airspy: {_airspy_error(lib, rc)}")
        self._dev = dev
        self._open = True
        try:
            self._check(lib.airspy_set_sample_type(dev, _AIRSPY_FLOAT32_IQ), "set_sample_type")
            rates = self._supported_rates()
            if not any(abs(r - sample_rate) < 1.0 for r in rates):
                raise RuntimeError("This Airspy captures only " + ", ".join(f"{r / 1e6:g}" for r in rates)
                                   + " MS/s - set the capture width to one of them")
            self._check(lib.airspy_set_samplerate(dev, int(round(sample_rate))), "set_samplerate")
            lib.airspy_set_rf_bias(dev, 0)
            self.set_gain(gain_mode, gain_db)
            self._lost_seen = 0
            self._cb = _AIRSPY_CB(self._on_data)
            self._check(lib.airspy_start_rx(dev, self._cb, None), "start_rx")
            if bias_tee:
                self._check(lib.airspy_set_rf_bias(dev, 1), "bias tee")
                self.bias_tee = True
        except Exception:
            self.close()
            raise

    def _check(self, rc: int, what: str) -> None:
        if rc != 0:
            raise RuntimeError(f"Airspy {what} failed: {_airspy_error(self._lib, rc)}")

    def _supported_rates(self) -> list[float]:
        count = ctypes.c_uint32(0)
        self._check(self._lib.airspy_get_samplerates(self._dev, ctypes.byref(count), 0), "get_samplerates")
        buf = (ctypes.c_uint32 * max(1, count.value))()
        self._check(self._lib.airspy_get_samplerates(self._dev, buf, count.value), "get_samplerates")
        return [float(r) for r in buf[:count.value]]

    def _on_data(self, transfer_p) -> int:
        try:
            t = transfer_p.contents
            if t.sample_count > 0 and t.sample_type == _AIRSPY_FLOAT32_IQ:
                self._stream.push(ctypes.string_at(t.samples, t.sample_count * 8), time.monotonic())
            if t.dropped_samples > self._lost_seen:     # cumulative, per the library's tools
                self._stream.add_lost(t.dropped_samples - self._lost_seen)
                self._lost_seen = t.dropped_samples
        except Exception as exc:  # noqa: BLE001
            self._stream.fail(f"Airspy callback failed: {exc}")
            return 1
        return 0

    def set_frequency(self, freq_hz: float) -> None:
        self._check(self._lib.airspy_set_freq(self._dev, int(freq_hz)), "set_freq")
        self._stream.begin_retune(time.monotonic())

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        lib, dev = self._lib, self._dev
        if gain_mode == "auto":     # the receiver's own AGC (LNA + mixer)
            self._check(lib.airspy_set_lna_agc(dev, 1), "set_lna_agc")
            self._check(lib.airspy_set_mixer_agc(dev, 1), "set_mixer_agc")
        else:                       # sets all three stages from the library's sensitivity table, AGC off
            index = int(round(max(_AIRSPY_GAIN_RANGE_DB[0], min(_AIRSPY_GAIN_RANGE_DB[1], gain_db))))
            self._check(lib.airspy_set_sensitivity_gain(dev, index), "set_sensitivity_gain")
        if getattr(self, "_cb", None) is not None:
            self._stream.begin_retune(time.monotonic())

    @staticmethod
    def to_iq(raw: np.ndarray) -> np.ndarray:
        return raw.view(np.complex64)

    def close(self) -> None:
        if not getattr(self, "_open", False):
            return
        self._open = False
        lib, dev = self._lib, self._dev
        for call in (lambda: lib.airspy_stop_rx(dev), lambda: lib.airspy_set_rf_bias(dev, 0)):
            try:
                call()
            except Exception:  # noqa: BLE001
                pass
        lib.airspy_close(dev)
        SoapyBackend._record_close_time()

    @staticmethod
    def enumerate_devices() -> list[dict]:
        lib = airspy_library()
        if lib is None:
            return []
        try:
            count = lib.airspy_list_devices(None, 0)
            if count <= 0:
                return []
            serials = (ctypes.c_uint64 * count)()
            count = lib.airspy_list_devices(serials, count)
        except Exception:  # noqa: BLE001
            return []
        return [{"driver": AIRSPY_NATIVE_DRIVER, "label": f"Airspy (libairspy) SN {serials[i]:016X}",
                 "serial": f"{serials[i]:016X}"} for i in range(max(0, count))]


def native_enumerate() -> list[dict]:
    return HackRfBackend.enumerate_devices() + AirspyBackend.enumerate_devices()


# USB ids of receivers that need more than the built-in RTL-SDR path (Linux; sysfs needs no library).
_USB_IDS = {("1d50", "6089"): "hackrf", ("1d50", "604b"): "hackrf", ("1d50", "cc15"): "hackrf",
            ("1d50", "60a1"): "airspy", ("03eb", "800c"): "airspyhf"}


def usb_receivers_seen() -> list[str]:
    """Families of plugged-in receivers, from /sys/bus/usb - so the UI can say "a HackRF is plugged in but
    nothing can drive it" instead of an empty device list (issue #9). Other systems: unknown, empty."""
    found: set[str] = set()
    try:
        for dev in Path("/sys/bus/usb/devices").iterdir():
            try:
                ids = ((dev / "idVendor").read_text().strip().lower(), (dev / "idProduct").read_text().strip().lower())
            except OSError:
                continue
            if ids in _USB_IDS:
                found.add(_USB_IDS[ids])
    except OSError:
        pass
    return sorted(found)
