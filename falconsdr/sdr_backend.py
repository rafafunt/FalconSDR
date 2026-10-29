"""SDR hardware: SoapySDR, native librtlsdr (Windows/macOS) and a simulated
device for testing without hardware.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import deque

import numpy as np

from .config import CONFIG_DIR
from .i18n import tr
from .io_diag_log import SdrIoDiagLogger

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
    HAVE_SOAPY = True
except ImportError:
    HAVE_SOAPY = False

# Native librtlsdr via pyrtlsdr (+ pyrtlsdrlib's bundled library) - the
# pip-only path for Windows/macOS, where SoapySDR has no pip wheel (gotcha 16).
# Broad except: a missing shared library raises OSError, not ImportError.
try:
    from rtlsdr import RtlSdr
    HAVE_RTLSDR_NATIVE = True
except Exception:  # noqa: BLE001
    HAVE_RTLSDR_NATIVE = False

#: config.device.driver value selecting RtlSdrBackend - deliberately distinct
#: from SoapySDR's own "rtlsdr" driver key, so both can coexist on Linux.
RTLSDR_NATIVE_DRIVER = "rtlsdr_native"


# Reopening the dongle within seconds of closing it has crashed the process
# natively (segfault in the C driver, no traceback). The close time is kept in
# a file - the next open may be a new process - and a too-early open waits.
LAST_CLOSE_FILE = CONFIG_DIR / "sdr_last_close.txt"
_REOPEN_COOLDOWN_S = 3.0


#: RF gain range (dB) until a real device reports its own.
DEFAULT_GAIN_RANGE_DB = (0.0, 50.0)


class SdrBackend(ABC):
    @abstractmethod
    def set_frequency(self, freq_hz: float) -> None: ...

    @abstractmethod
    def set_gain(self, gain_mode: str, gain_db: float) -> None: ...

    @abstractmethod
    def read(self, num_samples: int) -> np.ndarray: ...

    @abstractmethod
    def close(self) -> None: ...

    label = "unknown"
    gain_range_db: tuple[float, float] = DEFAULT_GAIN_RANGE_DB


# Bias-tee powers the antenna input - only when the user switched it on for
# this dongle (DeviceConfig.bias_tee, off by default), and off again on
# close: on the RTL-SDR Blog V4 the state persists across opens. Off = all
# three keys (names differ between SoapyRTLSDR versions; on the V4 "offset
# tuning" is the bias-tee line) on every (re)configure; on = the two real
# bias-tee keys - offset tuning changes how other tuners tune.
_BIAS_TEE_ARG_KEYS = ("biastee", "bias_tee", "offset_tuning")
_BIAS_TEE_ON_KEYS = ("biastee", "bias_tee")


class SoapyBackend(SdrBackend):
    label = "soapysdr"

    def __init__(self, driver: str, device_args: str, sample_rate: float, gain_mode: str, gain_db: float,
                 bias_tee: bool = False):
        if not HAVE_SOAPY:
            raise RuntimeError("The python3-soapysdr module is not installed")
        self._wait_for_reopen_cooldown()
        # "auto" always opens with an explicit rtlsdr filter, never {}: that
        # gave ~10 dB different power on the same dongle (gotcha 16).
        args = {"driver": driver if driver and driver != "auto" else "rtlsdr"}
        if device_args:
            for kv in device_args.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    k = k.strip()
                    if k in _BIAS_TEE_ARG_KEYS:
                        # Only the bias_tee setting powers the antenna - see _BIAS_TEE_ARG_KEYS.
                        continue
                    args[k] = v.strip()
        # A "k=v,k=v" string, not a dict: Ubuntu 26.04's python3-soapysdr
        # (0.8.1-7build1) silently matches no device when given a dict.
        args_str = ",".join(f"{k}={v}" for k, v in args.items())
        self.device = SoapySDR.Device(args_str)
        self.bias_tee = False  # on only once the device is fully set up (end of __init__)
        self._apply_bias_tee()
        self.sample_rate = sample_rate
        self.device.setSampleRate(SOAPY_SDR_RX, 0, sample_rate)
        try:
            rng = self.device.getGainRange(SOAPY_SDR_RX, 0)
            self.gain_range_db = (rng.minimum(), rng.maximum())
        except Exception:
            pass
        self.set_gain(gain_mode, gain_db)
        # "bufflen" is a SoapyRTLSDR *stream* arg and limits how much one
        # readStream() returns. Raw bytes (2 per 8-bit I/Q sample), a multiple
        # of 512; ~100 ms here, well above a 30 ms chunk.
        raw_bufflen = (int(sample_rate * 0.1) * 2 // 512 + 1) * 512
        try:
            self.stream = self.device.setupStream(
                SOAPY_SDR_RX, SOAPY_SDR_CF32, [0], {"bufflen": str(raw_bufflen)},
            )
        except Exception:
            # A driver that rejects the arg still opens - it's only a latency tweak.
            self.stream = self.device.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.device.activateStream(self.stream)
        try:
            self.stream_mtu = self.device.getStreamMTU(self.stream)
        except Exception:
            self.stream_mtu = None
        # Grown in read() so a whole chunk fits in one readStream() call.
        self._buf = np.empty(65536, dtype=np.complex64)
        self._io_diag = SdrIoDiagLogger()
        if bias_tee:
            self.bias_tee = True
            self._apply_bias_tee()

    def _apply_bias_tee(self, on: bool | None = None) -> None:
        on = self.bias_tee if on is None else on
        for key in _BIAS_TEE_ON_KEYS if on else _BIAS_TEE_ARG_KEYS:
            try:
                self.device.writeSetting(key, "true" if on else "false")
            except Exception:
                pass

    def set_frequency(self, freq_hz: float) -> None:
        self.device.setFrequency(SOAPY_SDR_RX, 0, freq_hz)
        # Some driver builds re-derive the bias-tee on retune - re-assert it.
        self._apply_bias_tee()

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        if gain_mode == "auto":
            try:
                self.device.setGainMode(SOAPY_SDR_RX, 0, True)
                return
            except Exception:
                pass
        try:
            self.device.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception:
            pass
        self.device.setGain(SOAPY_SDR_RX, 0, gain_db)

    def read(self, num_samples: int) -> np.ndarray:
        """Reads exactly ``num_samples`` or raises. The buffer grows to fit
        the whole chunk: a second readStream() call per tick doubled the read
        time (measured ~54 ms against a 30 ms budget).
        """
        if num_samples > self._buf.size:
            self._buf = np.empty(num_samples, dtype=np.complex64)
        out = np.empty(num_samples, dtype=np.complex64)
        got = 0
        calls = 0
        read_start = time.monotonic()
        deadline = read_start + 1.0
        while got < num_samples and time.monotonic() < deadline:
            chunk = min(num_samples - got, self._buf.size)
            sr = self.device.readStream(self.stream, [self._buf[:chunk]], chunk, timeoutUs=200_000)
            calls += 1
            if sr.ret > 0:
                out[got:got + sr.ret] = self._buf[:sr.ret]
                got += sr.ret
            elif sr.ret < 0:
                # Error code (e.g. overflow): what follows may not be contiguous
                # with what we have. Only logged for now (io_diag_log.py).
                self._io_diag.record_error(sr.ret, calls, got, num_samples)
        self._io_diag.record_read(time.monotonic() - read_start, calls)
        if got < num_samples:
            # Short after 1 s for a ~30 ms chunk: dead or starved stream. Raise
            # (gotcha 8), so a trickling dongle reconnects too instead of
            # scanning a truncated buffer forever.
            raise RuntimeError(
                f"SDR stream is delivering too little data ({got}/{num_samples} samples) "
                "- device disconnected or not responding"
            )
        return out[:got]

    def close(self) -> None:
        try:
            self.device.deactivateStream(self.stream)
            self.device.closeStream(self.stream)
        except Exception:
            pass
        self._apply_bias_tee(False)  # never leave the antenna powered once FalconSDR lets go
        self._record_close_time()

    @staticmethod
    def _record_close_time() -> None:
        """Best-effort: stores the close time for the next open's cooldown."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            LAST_CLOSE_FILE.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _wait_for_reopen_cooldown() -> None:
        try:
            last_close = float(LAST_CLOSE_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return  # no record - nothing to wait for
        remaining = _REOPEN_COOLDOWN_S - (time.time() - last_close)
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def enumerate_devices() -> list[dict]:
        if not HAVE_SOAPY:
            return []
        try:
            devices = [dict(r) for r in SoapySDR.Device.enumerate()]
        except Exception:
            return []
        # soapysdr-module-audio lists sound cards as SDRs (gotcha 4).
        return [d for d in devices if d.get("driver") != "audio"]


# SoapyRTLSDR's own CF32 mapping, so dBFS levels (and every tuned threshold)
# match across backends - pyrtlsdr's (i / 127.5 - 1) differs slightly.
_RTLSDR_LUT = ((np.arange(256, dtype=np.float32) - 127.4) / 128.0).astype(np.float32)

# Bulk transfers not a multiple of the 512 B USB packet cause overflows on
# some libusb backends; read() rounds up and carries the remainder over.
_RTLSDR_READ_GRANULE_BYTES = 512
_ASYNC_BUFFERS = 15                # 15 transfers = ~200 ms of IQ may wait in the USB queue
_ASYNC_BUFFER_S = 0.0136           # one transfer = this much IQ (65536 bytes at 2.4 MS/s)
_STREAM_STOP_TIMEOUT_S = 3.0
_ASYNC_AFTER_READS = 8             # blocking reads on one frequency before the stream takes over


class _StreamNotStarted(RuntimeError):
    """The async stream ended before it delivered anything."""


class _StreamStats:
    """How much of a stream's wall time its samples cover. A late callback is not a loss (the data
    waits in the USB queue), so the lag - wall time since the first callback minus the time the
    samples received so far span - is taken as its minimum per 2 s block, and the loss is how far
    that minimum has risen since the first block."""

    def __init__(self, sample_rate: float):
        self.rate = max(1.0, sample_rate)
        self.t0: float | None = None
        self.samples = 0
        self.first_min: float | None = None
        self.block_min = float("inf")
        self.block_end = 0.0
        self.lost_s = 0.0
        self.streamed_s = 0.0

    def add(self, now: float, nbytes: int) -> None:
        if self.t0 is None:
            self.t0, self.block_end = now, now + 2.0
        self.samples += nbytes // 2
        self.block_min = min(self.block_min, (now - self.t0) - self.samples / self.rate)
        if now >= self.block_end:
            if self.first_min is None:
                self.first_min = self.block_min
            else:
                self.lost_s = max(self.lost_s, self.block_min - self.first_min)
            self.streamed_s = now - self.t0
            self.block_min, self.block_end = float("inf"), now + 2.0


class RtlSdrBackend(SdrBackend):
    """RTL-SDR through librtlsdr directly (gotcha 16). Same contract as
    SoapyBackend: bias-tee off on open and every retune, reopen cooldown,
    read() raises instead of returning short. AcquisitionThread calls read_raw() back to back.

    Blocking reads (rtlsdr_read_sync) leave the dongle without a pending USB transfer between two
    calls, and it drops samples then (~1-3 % of the time, a splice every 30 ms: a buzz, and a voice
    pitched up by the lost fraction). So once the scanner has stayed on one frequency for
    _ASYNC_AFTER_READS reads - a transmission, a recording - a stream (rtlsdr_read_async: 15 x 64 KiB
    transfers always queued, ~200 ms of slack for a stalled process) takes over. It is stopped at
    every retune and gain step, which also throws away data of the old frequency; while hopping
    between windows the plain reads are kept, so scan speed is unchanged.
    """
    label = RTLSDR_NATIVE_DRIVER

    def __init__(self, device_args: str, sample_rate: float, gain_mode: str, gain_db: float,
                 bias_tee: bool = False, async_read: bool = True):
        if not HAVE_RTLSDR_NATIVE:
            raise RuntimeError("The pyrtlsdr/pyrtlsdrlib modules are not installed")
        self._async = bool(async_read)
        self._sync_reads = 0             # blocking reads since the last retune/gain step
        self._stream_thread: threading.Thread | None = None
        self._stream_cb = None           # the ctypes callback must outlive the stream
        self._stream_cond = threading.Condition()
        self._stream_chunks: deque[bytes] = deque()
        self._stream_error: int | None = None
        self._stream_stopping = False
        self._stream_stats = _StreamStats(sample_rate)
        SoapyBackend._wait_for_reopen_cooldown()
        args = dict(kv.split("=", 1) for kv in (device_args or "").split(",") if "=" in kv)
        args = {k.strip(): v.strip() for k, v in args.items()}
        if args.get("index", "").isdigit():  # dongles without a serial of their own (enumerate_devices)
            self.device = RtlSdr(device_index=int(args["index"]))
        elif args.get("serial"):
            self.device = RtlSdr(serial_number=args["serial"])
        else:
            self.device = RtlSdr()
        self.bias_tee = False  # on only once the device is fully set up (below)
        try:
            self.device.set_bias_tee(False)
            self.sample_rate = sample_rate
            self.device.sample_rate = sample_rate
            gains = self.device.get_gains()  # tenths of a dB
            if gains:
                self.gain_range_db = (min(gains) / 10.0, max(gains) / 10.0)
            self.set_gain(gain_mode, gain_db)
        except Exception:
            self.device.close()
            raise
        self._carry = np.empty(0, dtype=np.uint8)
        self._io_diag = SdrIoDiagLogger()
        if bias_tee:
            self.bias_tee = True
            self.device.set_bias_tee(True)

    def set_frequency(self, freq_hz: float) -> None:
        self._stop_stream()
        self.device.center_freq = int(freq_hz)
        # See SoapyBackend.set_frequency() - re-asserted on every retune.
        self.device.set_bias_tee(self.bias_tee)
        # Leftover bytes belong to the previous frequency.
        self._carry = np.empty(0, dtype=np.uint8)

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        self._stop_stream()  # a gain step stalls the dongle anyway
        # pyrtlsdr's set_gain() already picks the nearest supported step.
        self.device.set_gain("auto" if gain_mode == "auto" else gain_db)

    def read(self, num_samples: int) -> np.ndarray:
        return self.to_iq(self.read_raw(num_samples))

    @staticmethod
    def to_iq(raw: np.ndarray) -> np.ndarray:
        """Raw unsigned 8-bit I/Q bytes -> CF32 (the same scaling as SoapyBackend)."""
        return _RTLSDR_LUT[raw].view(np.complex64)

    def read_raw(self, num_samples: int) -> np.ndarray:
        """The next 2 * num_samples bytes, unconverted. AcquisitionThread calls this and converts on
        the consumer's side: the conversion takes ~0.4 ms, and during that time no transfer is
        outstanding, so the dongle's FIFO overflows - ~1.5 % of the samples lost, a splice in the IQ
        every 30 ms (audible as a buzz, and the audio stream runs short of real time)."""
        if self._async and self._stream_thread is None and self._sync_reads >= _ASYNC_AFTER_READS:
            try:
                self._start_stream()
            except Exception as exc:  # e.g. the library can't do it: keep scanning with blocking reads
                self._give_up_streaming(exc)
        need = 2 * num_samples - self._carry.size
        read_start = time.monotonic()
        if need > 0:
            raw = None
            if self._stream_thread is not None:
                try:
                    raw = self._read_stream(need)
                except _StreamNotStarted as exc:
                    self._give_up_streaming(exc)
            if raw is None:
                self._sync_reads += 1
                request = -(-need // _RTLSDR_READ_GRANULE_BYTES) * _RTLSDR_READ_GRANULE_BYTES
                try:
                    # Raises on an unplugged device or a short read, like SoapyBackend.
                    raw = np.ctypeslib.as_array(self.device.read_bytes(request)).copy()
                except Exception as exc:
                    raise RuntimeError(f"RTL-SDR read failed - device disconnected or not responding ({exc})") from exc
            data = np.concatenate((self._carry, raw)) if self._carry.size else raw
        else:
            data = self._carry
        self._io_diag.record_read(time.monotonic() - read_start, 1)
        used = 2 * num_samples
        self._carry = data[used:]
        return data[:used]

    # -- async stream ----------------------------------------------------
    def _give_up_streaming(self, reason: Exception) -> None:
        """The stream could not start (it never delivered a byte): from now on blocking reads only.
        Raising instead would make the engine reconnect and fail again, forever, on such a machine."""
        print(f"[rtlsdr] async stream unavailable ({reason}) - using blocking reads", flush=True)
        self._async = False
        self._stream_thread = None
        with self._stream_cond:
            self._stream_chunks.clear()

    @staticmethod
    def _async_buffer_bytes(sample_rate: float) -> int:
        """One transfer = ~13.6 ms of IQ whatever the rate (a multiple of 512): fixed 64 KiB would
        make 250 kS/s arrive in 131 ms bursts."""
        return max(4096, min(262144, round(sample_rate * 2 * _ASYNC_BUFFER_S / 512) * 512))

    def _start_stream(self) -> None:
        from rtlsdr.librtlsdr import librtlsdr, rtlsdr_read_async_cb_t
        dev_p = self.device.dev_p
        with self._stream_cond:
            self._stream_chunks.clear()
            self._stream_error, self._stream_stopping = None, False
        self._stream_stats = _StreamStats(self.sample_rate)
        stats = self._stream_stats
        buffer_bytes = self._async_buffer_bytes(self.sample_rate)

        def on_data(buf, length, _ctx):  # runs in the stream thread, GIL held: copy and queue only
            data = ctypes.string_at(buf, length)
            stats.add(time.monotonic(), length)
            with self._stream_cond:
                self._stream_chunks.append(data)
                self._stream_cond.notify()

        self._stream_cb = rtlsdr_read_async_cb_t(on_data)
        callback = self._stream_cb

        def run() -> None:
            result = librtlsdr.rtlsdr_read_async(dev_p, callback, self, _ASYNC_BUFFERS, buffer_bytes)
            with self._stream_cond:
                if not self._stream_stopping:
                    self._stream_error = result if result < 0 else -1  # ended by itself = the device is gone
                self._stream_cond.notify_all()

        librtlsdr.rtlsdr_reset_buffer(dev_p)
        self._stream_thread = threading.Thread(target=run, name="rtlsdr-stream", daemon=True)
        self._stream_thread.start()

    def _stop_stream(self) -> None:
        thread = getattr(self, "_stream_thread", None)
        self._sync_reads = 0
        if thread is None:
            return
        from rtlsdr.librtlsdr import librtlsdr
        with self._stream_cond:
            self._stream_stopping = True
        deadline = time.monotonic() + _STREAM_STOP_TIMEOUT_S
        while thread.is_alive() and time.monotonic() < deadline:
            librtlsdr.rtlsdr_cancel_async(self.device.dev_p)  # fails until the stream is really running: retry
            thread.join(0.05)
        self._stream_thread = None
        with self._stream_cond:
            self._stream_chunks.clear()
        if thread.is_alive():
            raise RuntimeError("RTL-SDR stream did not stop - device not responding")

    def _read_stream(self, need: int) -> np.ndarray:
        parts: list[bytes] = []
        got = 0
        deadline = time.monotonic() + 1.0
        with self._stream_cond:
            while got < need:
                if self._stream_chunks:
                    chunk = self._stream_chunks.popleft()
                    parts.append(chunk)
                    got += len(chunk)
                    continue
                if self._stream_error is not None and self._stream_stats.samples == 0:
                    raise _StreamNotStarted(f"error {self._stream_error}")
                if self._stream_error is not None:
                    raise RuntimeError(f"RTL-SDR stream failed - device disconnected or not responding ({self._stream_error})")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("RTL-SDR stream delivered no data - device disconnected or not responding")
                self._stream_cond.wait(remaining)
        return np.frombuffer(b"".join(parts), dtype=np.uint8)

    @property
    def streaming(self) -> bool:
        return self._stream_thread is not None

    def stream_stats(self) -> tuple[float, float]:
        """(seconds of the last/current stream that no sample covers, seconds it ran)."""
        return self._stream_stats.lost_s, self._stream_stats.streamed_s

    def close(self) -> None:
        try:
            self._stop_stream()
            stream_stuck = False
        except Exception:
            stream_stuck = True
        try:
            self.device.set_bias_tee(False)  # never leave the antenna powered once FalconSDR lets go
        except Exception:
            pass
        if not stream_stuck:  # freeing the device under a running rtlsdr_read_async could crash the process
            try:
                self.device.close()
            except Exception:
                pass
        SoapyBackend._record_close_time()

    @staticmethod
    def enumerate_devices() -> list[dict]:
        if not HAVE_RTLSDR_NATIVE:
            return []
        try:
            from rtlsdr.librtlsdr import librtlsdr
            count = librtlsdr.rtlsdr_get_device_count()
        except Exception:
            return []
        # One by one: pyrtlsdr's get_device_serial_addresses() fails as a whole
        # when one dongle can't be opened (in use, no WinUSB driver) - reported
        # with 10 dongles on Windows, none could be picked.
        serials: list[str | None] = []
        for i in range(count):
            try:
                serials.append(_read_serial(librtlsdr, i))
            except Exception:
                serials.append(None)
        devices = []
        for i, serial in enumerate(serials):
            # No commas in the label - Controller.devices() joins every key into one
            # "k=v,k=v" device_args string.
            if serial and serials.count(serial) == 1:  # by serial: survives replugging
                devices.append({"driver": RTLSDR_NATIVE_DRIVER, "label": f"RTL-SDR (librtlsdr) SN {serial}", "serial": serial})
                continue
            # Most dongles ship with serial 00000001 - only the USB order tells them apart.
            label = (tr("RTL-SDR #{n} (librtlsdr) SN {serial} - same serial as another dongle").format(n=i + 1, serial=serial)
                     if serial else tr("RTL-SDR #{n} (librtlsdr) - in use or no WinUSB driver").format(n=i + 1))
            devices.append({"driver": RTLSDR_NATIVE_DRIVER, "label": label, "index": str(i)})
        return devices


def _read_serial(lib, index: int) -> str:
    """One dongle's USB serial string (raises if it can't be opened)."""
    from ctypes import c_ubyte
    buf = (c_ubyte * 256)()
    if lib.rtlsdr_get_device_usb_strings(index, None, None, buf) != 0:
        raise OSError(f"can't read RTL-SDR #{index + 1}")
    return bytes(b for b in buf if b).decode("ascii", "replace")


def enumerate_devices() -> list[dict]:
    """Every real device to offer. Native librtlsdr devices only when
    SoapySDR shows no rtlsdr one, or the same dongle is listed twice."""
    devices = SoapyBackend.enumerate_devices()
    if not any(d.get("driver") == "rtlsdr" for d in devices):
        devices += RtlSdrBackend.enumerate_devices()
    return devices


def open_real_backend(driver: str, device_args: str, sample_rate: float, gain_mode: str, gain_db: float,
                      bias_tee: bool = False, async_read: bool = True) -> SdrBackend:
    """Opens real hardware only - never falls back to SimulatedBackend.

    "auto" tries SoapySDR first (Linux's native path), then librtlsdr
    directly. A config saved on Linux with SoapySDR's "rtlsdr" driver also
    opens through librtlsdr on a machine without SoapySDR, so the same
    config.json works across platforms."""
    if driver == RTLSDR_NATIVE_DRIVER or (driver == "rtlsdr" and not HAVE_SOAPY):
        return RtlSdrBackend(device_args, sample_rate, gain_mode, gain_db, bias_tee, async_read)
    if driver and driver != "auto":
        return SoapyBackend(driver, device_args, sample_rate, gain_mode, gain_db, bias_tee)
    if not HAVE_SOAPY and HAVE_RTLSDR_NATIVE:
        return RtlSdrBackend(device_args, sample_rate, gain_mode, gain_db, bias_tee, async_read)
    try:
        return SoapyBackend(driver, device_args, sample_rate, gain_mode, gain_db, bias_tee)
    except Exception as soapy_exc:
        if not HAVE_RTLSDR_NATIVE:
            raise
        try:
            return RtlSdrBackend(device_args, sample_rate, gain_mode, gain_db, bias_tee, async_read)
        except Exception:
            raise soapy_exc


class SimulatedBackend(SdrBackend):
    """Generates noise plus occasional synthetic AM bursts so the whole
    scan/detect/record pipeline can be exercised without real hardware.
    """
    label = "simulated"

    # Frequencies (Hz) that will "transmit" a fake burst when tuned to.
    _ACTIVE_FREQS = {121_500_000, 118_100_000, 124_000_000}
    _PERIOD_S = 5.0
    _ON_DURATION_S = 2.5

    def __init__(self, sample_rate: float):
        self.sample_rate = sample_rate
        self.freq_hz = 0.0
        self._rng = np.random.default_rng()
        self._t = 0.0
        self._start_time = time.monotonic()
        self._gain_scale = 1.0

    def set_frequency(self, freq_hz: float) -> None:
        self.freq_hz = freq_hz

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        # Scale the fake signal so the gain control visibly does something.
        reference_db = 30.0
        self._gain_scale = 10 ** ((gain_db - reference_db) / 20.0)

    def _bursting_now(self, active_freq: int) -> bool:
        # A function of wall-clock time only (gotcha 3) - retuning can't cut a burst short.
        offset = (hash(active_freq) % 97) / 97.0 * self._PERIOD_S
        elapsed = time.monotonic() - self._start_time + offset
        return (elapsed % self._PERIOD_S) < self._ON_DURATION_S

    def read(self, num_samples: int) -> np.ndarray:
        # Real-time pacing, like a real device (gotcha 3).
        dur = num_samples / self.sample_rate
        time.sleep(dur)

        noise = (self._rng.normal(size=num_samples) + 1j * self._rng.normal(size=num_samples)).astype(np.complex64)
        noise *= 0.01

        # Any active frequency inside the tuned window appears at its offset (gotcha 9).
        half_bw = self.sample_rate / 2.0 * 0.95
        tt = self._t + np.arange(num_samples) / self.sample_rate
        for active_freq in self._ACTIVE_FREQS:
            offset_hz = active_freq - self.freq_hz
            if abs(offset_hz) > half_bw or not self._bursting_now(active_freq):
                continue
            tone = 0.6 * np.sin(2 * np.pi * 800 * tt)
            envelope = (1.0 + tone) * 0.5
            carrier = np.exp(1j * 2 * np.pi * offset_hz * tt)
            signal = 0.5 * envelope * carrier
            noise = noise + signal.astype(np.complex64)
        self._t += dur
        return (noise * self._gain_scale).astype(np.complex64)

    def close(self) -> None:
        pass


class AcquisitionThread:
    """Reads the backend back-to-back on its own thread into a bounded queue
    (gotcha 11), so slow processing can never delay the next hardware read.
    A late read lets the driver's ~100 ms ring buffer overflow and silently
    splice non-contiguous IQ - audible clicks in live audio and recordings
    (measured: 14 overflows in 35 s on a busy FM band). read() keeps the
    SdrBackend contract.

    ``max_queued`` bounds the queue (several seconds): if the consumer falls
    that far behind, the oldest chunk is dropped and counted.
    """

    def __init__(self, backend: "SdrBackend", chunk_size: int, max_queued: int = 64, debug: bool = False):
        self._backend = backend
        # A backend whose conversion to IQ is worth keeping out of the read loop (RtlSdrBackend)
        # offers read_raw()/to_iq(); the queue then holds raw chunks and read() converts them.
        self._read_chunk = getattr(backend, "read_raw", backend.read)
        self._to_iq = getattr(backend, "to_iq", None) if hasattr(backend, "read_raw") else None
        self._chunk_size = chunk_size
        self._max_queued = max_queued
        # Prints the RT-priority outcome and health summary (gotcha 29/32).
        self._debug = debug
        self._queue: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop_evt = threading.Event()
        self._error: BaseException | None = None
        # Always-on health counters - a dropped chunk is real data loss that
        # nothing downstream can detect (gotcha 30).
        self._chunks_read = 0
        self._chunks_dropped = 0
        self._read_slow_count = 0  # read() calls that took > one chunk's own duration
        self._read_max_s = 0.0
        self._chunk_duration_s = chunk_size / max(1.0, backend.sample_rate)
        self._health_reported_at = time.monotonic()
        # Wall time that no IQ covers. A sync read leaves the dongle with no transfer pending
        # between two calls, so it drops samples then: the time from one read's start to the next
        # minus the chunk's own duration is what was lost. A retune/gain step restarts the chain.
        self._prev_read_start: float | None = None
        self._reads_since_cmd = 0
        self._settle_lost_s = 0.0
        self._iq_lost_s = 0.0
        self._iq_streamed_s = 0.0
        # Bumped on every applied retune/gain change - see _run().
        self._generation = 0
        # Only this thread touches the backend: set_frequency()/set_gain()
        # hand it a command. A lock shared with this SCHED_FIFO thread starved
        # a retune for 11.7 s (priority inversion).
        self._pending_command: tuple | None = None
        self._command_done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sdr-acquisition", daemon=True)
        self._thread.start()

    def _boost_priority(self) -> None:
        """Best-effort SCHED_FIFO, like ScannerEngine._maybe_boost_priority().
        Always attempted; in debug mode the outcome is printed - a refused
        boost (no rtprio limit, common when started from a desktop icon) is
        otherwise invisible (gotcha 29)."""
        if not hasattr(os, "sched_setscheduler"):
            if self._debug:
                print("[sdr-acquisition] real-time priority: unavailable on this platform", flush=True)
            return
        try:
            tid = threading.get_native_id()
            os.sched_setscheduler(tid, os.SCHED_FIFO, os.sched_param(10))
        except (PermissionError, OSError) as exc:
            if self._debug:
                print(
                    f"[sdr-acquisition] real-time priority NOT granted ({exc}) - running at normal "
                    "priority; check `ulimit -r` (needs >0) if live audio/scan speed seems inconsistent",
                    flush=True,
                )
        else:
            if self._debug:
                print("[sdr-acquisition] real-time priority: OK (SCHED_FIFO)", flush=True)

    @property
    def chunks_dropped(self) -> int:
        return self._chunks_dropped

    _HEALTH_REPORT_INTERVAL_S = 20.0

    def _maybe_report_health(self) -> None:
        """Debug mode: periodic read-timing and drop counters (gotcha 30).
        chunks_DROPPED tells real data loss apart from mere timing jitter.
        Cumulative, so a single drop never ages out of view."""
        if not self._debug:
            return
        now = time.monotonic()
        if now - self._health_reported_at < self._HEALTH_REPORT_INTERVAL_S:
            return
        self._health_reported_at = now
        print(
            f"[sdr-acquisition] health: chunks_read={self._chunks_read} "
            f"chunks_DROPPED={self._chunks_dropped} slow_reads={self._read_slow_count} "
            f"(budget {self._chunk_duration_s * 1000:.0f}ms) max_read={self._read_max_s * 1000:.0f}ms "
            f"queue_depth={len(self._queue)}/{self._max_queued} "
            f"iq_uncovered(steady)={self._iq_lost_s:.2f}s of {self._iq_streamed_s:.0f}s "
            f"({100.0 * self._iq_lost_s / max(1e-9, self._iq_streamed_s):.2f}%) "
            f"retune_settling={self._settle_lost_s:.1f}s"
            f"{self._stream_report()}",
            flush=True,
        )

    def _stream_report(self) -> str:
        stats = getattr(self._backend, "stream_stats", None)
        if stats is None:
            return ""
        lost, streamed = stats()
        return f" async_stream(last)={lost:.2f}s of {streamed:.0f}s ({100.0 * lost / max(1e-9, streamed):.2f}%)"

    _SETTLE_READS = 8  # ~240 ms after a retune/gain step the dongle is still catching up

    def _note_read_start(self, read_start: float) -> None:
        """Debug-mode bookkeeping for the time between two reads (see __init__). Only a steady
        stream (the scanner parked on one frequency: a transmission, a recording) is reported -
        scanning retunes all the time and every retune costs ~100 ms by design."""
        if getattr(self._backend, "streaming", False):
            # Reads come out of the stream's queue in 13.6 ms steps: their spacing says nothing
            # about lost samples (the backend's stream_stats() does).
            self._prev_read_start = None
            return
        prev, self._prev_read_start = self._prev_read_start, read_start
        self._reads_since_cmd = 1 if prev is None else self._reads_since_cmd + 1
        if prev is None:
            return
        lost = max(0.0, read_start - prev - self._chunk_duration_s)
        if self._reads_since_cmd <= self._SETTLE_READS:
            self._settle_lost_s += lost
            return
        self._iq_lost_s += lost
        self._iq_streamed_s += self._chunk_duration_s

    def _run(self) -> None:
        self._boost_priority()
        while not self._stop_evt.is_set():
            # Apply a pending retune/gain change before the next read.
            with self._cond:
                command = self._pending_command
                self._pending_command = None
                my_generation = self._generation
            if command is not None:
                kind, args = command
                try:
                    if kind == "freq":
                        self._backend.set_frequency(*args)
                    else:
                        self._backend.set_gain(*args)
                except BaseException as exc:  # noqa: BLE001 - propagated to the waiting caller below
                    with self._cond:
                        self._error = exc
                        self._cond.notify_all()
                    self._command_done.set()
                    return
                with self._cond:
                    self._queue.clear()
                    self._prev_read_start = None  # the retune/gain step itself is no loss to report
                    self._generation += 1
                    my_generation = self._generation
                self._command_done.set()
            try:
                read_start = time.monotonic()
                self._note_read_start(read_start)
                block = self._read_chunk(self._chunk_size)
                read_elapsed = time.monotonic() - read_start
            except BaseException as exc:  # noqa: BLE001 - propagated verbatim to read(), see there
                with self._cond:
                    self._error = exc
                    self._cond.notify_all()
                # Wake a waiting set_frequency()/set_gain() with the real error.
                self._command_done.set()
                return
            with self._cond:
                if self._stop_evt.is_set():
                    return
                if my_generation != self._generation:
                    # Read before a retune landed: the previous window's IQ.
                    # Kept, it caused false detections in the new window.
                    continue
                self._chunks_read += 1
                if read_elapsed > self._chunk_duration_s:
                    self._read_slow_count += 1
                self._read_max_s = max(self._read_max_s, read_elapsed)
                self._queue.append(block)
                if len(self._queue) > self._max_queued:
                    self._queue.popleft()
                    self._chunks_dropped += 1
                self._cond.notify_all()
            self._maybe_report_health()

    def read(self, num_samples: int, timeout: float = 1.5) -> np.ndarray:
        """SdrBackend.read() contract: blocks for the next chunk, re-raises
        whatever killed the thread (so reconnect works unchanged).
        ``num_samples`` must equal the chunk size it was started with."""
        if num_samples != self._chunk_size:
            raise ValueError(
                f"AcquisitionThread was started with chunk_size={self._chunk_size}, "
                f"but read({num_samples}) was requested - these must match."
            )
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._queue and self._error is None and not self._stop_evt.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "SDR acquisition thread didn't deliver data in time - "
                        "device disconnected or not responding"
                    )
                self._cond.wait(remaining)
            if self._error is not None:
                raise self._error
            if not self._queue:
                raise RuntimeError("SDR acquisition thread stopped")
            block = self._queue.popleft()
        return self._to_iq(block) if self._to_iq is not None else block

    def _submit_command(self, kind: str, args: tuple, timeout: float = 2.0) -> None:
        """Hands a retune/gain change to the acquisition thread (see
        __init__) and waits until it's applied - about one read() - or
        raises on timeout or a dead device."""
        self._command_done.clear()
        with self._cond:
            if self._error is not None:
                raise self._error
            self._pending_command = (kind, args)
            self._cond.notify_all()
        if not self._command_done.wait(timeout) and self._error is None:
            raise RuntimeError(
                "SDR acquisition thread didn't respond to the frequency/gain change in time"
            )
        if self._error is not None:
            raise self._error

    def set_frequency(self, freq_hz: float) -> None:
        self._submit_command("freq", (freq_hz,))

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        self._submit_command("gain", (gain_mode, gain_db))

    def close(self) -> None:
        self._stop_evt.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=2.0)
        self._backend.close()

    @property
    def label(self) -> str:
        return self._backend.label

    @property
    def gain_range_db(self) -> tuple[float, float]:
        return self._backend.gain_range_db

    @property
    def stream_mtu(self):
        return getattr(self._backend, "stream_mtu", None)


def create_backend(
    driver: str, device_args: str, sample_rate: float, gain_mode: str, gain_db: float,
    debug_mode: bool = False, bias_tee: bool = False, async_read: bool = True,
) -> SdrBackend:
    if driver == "simulated":
        backend = SimulatedBackend(sample_rate)
        backend.set_gain(gain_mode, gain_db)
        return backend

    if not driver or driver == "auto":
        # Simulate only in debug mode (gotcha 8): a device that failed to open
        # must show an error, not look like a working scan.
        try:
            return open_real_backend(driver, device_args, sample_rate, gain_mode, gain_db, bias_tee, async_read)
        except Exception:
            if not debug_mode:
                raise
            backend = SimulatedBackend(sample_rate)
            backend.set_gain(gain_mode, gain_db)
            return backend

    # An explicitly chosen device that fails must always raise.
    return open_real_backend(driver, device_args, sample_rate, gain_mode, gain_db, bias_tee, async_read)
