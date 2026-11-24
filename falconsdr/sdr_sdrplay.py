"""Experimental native SDRplay backend (RSP1/1A/1B/2/duo/dx/dx-R2) through the SDRplay API 3.15, loaded with ctypes.

The API is two halves that must be the same version: a service (installed with admin rights, starts with the
system; on Windows it also brings the WinUSB driver) and the library an application loads. So FalconSDR never
ships the library - the user installs the free "SDRplay API" from sdrplay.com once, and FalconSDR finds it:
Windows through the registry (HKLM\\SOFTWARE\\SDRplay\\Service\\API\\Install_Dir), elsewhere /usr/local/lib, or
FALCONSDR_LIBSDRPLAY. The structures below are transcribed from the 3.15 headers; 3.08 -> 3.15 changed their
layout (RspDuoTunerParamsT grew), so an older API is refused rather than written into at the wrong offsets
(AGENTS.md gotcha 62). Never run against a real RSP by the author: ``main.py --probe-sdr`` and the receiver report
measure what this can only assume.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import sys
import threading
import time

import numpy as np

from .sdr_backend import SoapyBackend
from .sdr_native import CallbackStream, _NativeStreamBackend

SDRPLAY_NATIVE_DRIVER = "sdrplay_native"
MIN_API_VERSION = 3.15
API_DOWNLOAD_URL = "https://www.sdrplay.com/api/"
SAMPLE_RATES_HZ = (2_000_000.0, 10_000_000.0)    # without the API's decimation
DEFAULT_RATE_HZ = 6_000_000                      # 5 MHz analog bandwidth: the airband in 4 windows
MODELS = {1: "RSP1", 255: "RSP1A", 6: "RSP1B", 2: "RSP2", 3: "RSPduo", 4: "RSPdx", 7: "RSPdx-R2"}
_RETUNE_WAIT_S = 0.25    # longest wait for the stream's own "new frequency from here" flag
_GAIN_WAIT_S = 0.1

c = ctypes
_uc, _int, _uint, _ushort, _double, _float = c.c_ubyte, c.c_int, c.c_uint, c.c_ushort, c.c_double, c.c_float


# ----------------------------------------------------------------------- structures (sdrplay_api*.h, 3.15)
class _FsFreq(c.Structure):
    _fields_ = [("fsHz", _double), ("syncUpdate", _uc), ("reCal", _uc)]


class _SyncUpdate(c.Structure):
    _fields_ = [("sampleNum", _uint), ("period", _uint)]


class _ResetFlags(c.Structure):
    _fields_ = [("resetGainUpdate", _uc), ("resetRfUpdate", _uc), ("resetFsUpdate", _uc)]


class _Rsp1aParams(c.Structure):
    _fields_ = [("rfNotchEnable", _uc), ("rfDabNotchEnable", _uc)]


class _Rsp2Params(c.Structure):
    _fields_ = [("extRefOutputEn", _uc)]


class _RspDuoParams(c.Structure):
    _fields_ = [("extRefOutputEn", _int)]


class _RspDxParams(c.Structure):
    _fields_ = [("hdrEnable", _uc), ("biasTEnable", _uc), ("antennaSel", _int), ("rfNotchEnable", _uc),
                ("rfDabNotchEnable", _uc)]


class _DevParams(c.Structure):
    _fields_ = [("ppm", _double), ("fsFreq", _FsFreq), ("syncUpdate", _SyncUpdate), ("resetFlags", _ResetFlags),
                ("mode", _int), ("samplesPerPkt", _uint), ("rsp1aParams", _Rsp1aParams), ("rsp2Params", _Rsp2Params),
                ("rspDuoParams", _RspDuoParams), ("rspDxParams", _RspDxParams)]


class _GainValues(c.Structure):
    _fields_ = [("curr", _float), ("max", _float), ("min", _float)]


class _Gain(c.Structure):
    _fields_ = [("gRdB", _int), ("LNAstate", _uc), ("syncUpdate", _uc), ("minGr", _int), ("gainVals", _GainValues)]


class _RfFreq(c.Structure):
    _fields_ = [("rfHz", _double), ("syncUpdate", _uc)]


class _DcOffsetTuner(c.Structure):
    _fields_ = [("dcCal", _uc), ("speedUp", _uc), ("trackTime", _int), ("refreshRateTime", _int)]


class _TunerParams(c.Structure):
    _fields_ = [("bwType", _int), ("ifType", _int), ("loMode", _int), ("gain", _Gain), ("rfFreq", _RfFreq),
                ("dcOffsetTuner", _DcOffsetTuner)]


class _DcOffset(c.Structure):
    _fields_ = [("DCenable", _uc), ("IQenable", _uc)]


class _Decimation(c.Structure):
    _fields_ = [("enable", _uc), ("decimationFactor", _uc), ("wideBandSignal", _uc)]


class _Agc(c.Structure):
    _fields_ = [("enable", _int), ("setPoint_dBfs", _int), ("attack_ms", _ushort), ("decay_ms", _ushort),
                ("decay_delay_ms", _ushort), ("decay_threshold_dB", _ushort), ("syncUpdate", _int)]


class _ControlParams(c.Structure):
    _fields_ = [("dcOffset", _DcOffset), ("decimation", _Decimation), ("agc", _Agc), ("adsbMode", _int)]


class _Rsp1aTunerParams(c.Structure):
    _fields_ = [("biasTEnable", _uc)]


class _Rsp2TunerParams(c.Structure):
    _fields_ = [("biasTEnable", _uc), ("amPortSel", _int), ("antennaSel", _int), ("rfNotchEnable", _uc)]


class _RspDuoResetSlaveFlags(c.Structure):
    _fields_ = [("resetGainUpdate", _uc), ("resetRfUpdate", _uc)]


class _RspDuoTunerParams(c.Structure):
    _fields_ = [("biasTEnable", _uc), ("tuner1AmPortSel", _int), ("tuner1AmNotchEnable", _uc), ("rfNotchEnable", _uc),
                ("rfDabNotchEnable", _uc), ("resetSlaveFlags", _RspDuoResetSlaveFlags)]


class _RspDxTunerParams(c.Structure):
    _fields_ = [("hdrBw", _int)]


class _RxChannelParams(c.Structure):
    _fields_ = [("tunerParams", _TunerParams), ("ctrlParams", _ControlParams), ("rsp1aTunerParams", _Rsp1aTunerParams),
                ("rsp2TunerParams", _Rsp2TunerParams), ("rspDuoTunerParams", _RspDuoTunerParams),
                ("rspDxTunerParams", _RspDxTunerParams)]


class _Device(c.Structure):
    _fields_ = [("SerNo", c.c_char * 64), ("hwVer", _uc), ("tuner", _int), ("rspDuoMode", _int), ("valid", _uc),
                ("rspDuoSampleFreq", _double), ("dev", c.c_void_p)]


class _DeviceParams(c.Structure):
    _fields_ = [("devParams", c.POINTER(_DevParams)), ("rxChannelA", c.POINTER(_RxChannelParams)),
                ("rxChannelB", c.POINTER(_RxChannelParams))]


class _StreamCbParams(c.Structure):
    _fields_ = [("firstSampleNum", _uint), ("grChanged", _int), ("rfChanged", _int), ("fsChanged", _int),
                ("numSamples", _uint)]


class _GainCbParam(c.Structure):
    _fields_ = [("gRdB", _uint), ("lnaGRdB", _uint), ("currGain", _double)]


class _EventParams(c.Union):
    _fields_ = [("gainParams", _GainCbParam), ("powerOverloadChangeType", _int), ("rspDuoModeChangeType", _int)]


_STREAM_CB = c.CFUNCTYPE(None, c.POINTER(c.c_short), c.POINTER(c.c_short), c.POINTER(_StreamCbParams), _uint, _uint, c.c_void_p)
_EVENT_CB = c.CFUNCTYPE(None, _int, _int, c.POINTER(_EventParams), c.c_void_p)


class _CallbackFns(c.Structure):
    _fields_ = [("StreamACbFn", _STREAM_CB), ("StreamBCbFn", _STREAM_CB), ("EventCbFn", _EVENT_CB)]


# Constants (the header's enums and #defines)
_SUCCESS, _SERVICE_NOT_RESPONDING = 0, 14
_TUNER_A, _TUNER_B = 1, 2
_DUO_SINGLE_TUNER = 1
_IF_ZERO, _LO_AUTO = 0, 1
_AGC_DISABLE, _AGC_CTRL_EN = 0, 4
_UPD_RSP1A_BIAST, _UPD_RSP2_BIAST, _UPD_RSP2_AMPORT, _UPD_RSP2_ANTENNA = 0x10, 0x80, 0x100, 0x200
_UPD_TUNER_GR, _UPD_TUNER_FRF, _UPD_AGC, _UPD_OVERLOAD_ACK = 0x8000, 0x20000, 0x01000000, 0x04000000
_UPD_DUO_BIAST, _UPD_DUO_AMPORT = 0x08000000, 0x10000000
_EXT_DX_BIAST, _EXT_DX_ANTENNA = 0x2, 0x4
_EV_POWER_OVERLOAD, _EV_DEVICE_REMOVED, _EV_DEVICE_FAILURE = 1, 2, 4
_OVERLOAD_DETECTED = 0
_RSP2_ANTENNA_A, _RSP2_ANTENNA_B, _AMPORT_1, _AMPORT_2 = 5, 6, 1, 0
_BANDWIDTHS_KHZ = (200, 300, 600, 1536, 5000, 6000, 7000, 8000)

# Antenna names as SoapySDRPlay3 calls them, so one config.json works through either way of opening the RSP.
ANTENNAS = {2: ("Antenna A", "Antenna B", "Hi-Z"), 3: ("Tuner 1 50 ohm", "Tuner 2 50 ohm", "Tuner 1 Hi-Z"),
            4: ("Antenna A", "Antenna B", "Antenna C"), 7: ("Antenna A", "Antenna B", "Antenna C")}


# ----------------------------------------------------------------------- gain (API spec 3.15, section 5)
# LNA gain reduction in dB per LNA state, by frequency band (upper edge in MHz). The PDF prints footnote marks
# glued to the numbers ("612" = 61 with note 2); each row's length matches the header's *_NUM_LNA_STATES.
_LOW = [0, 6, 12, 18, 37, 42, 61]
_VHF = [0, 6, 12, 18, 20, 26, 32, 38, 57, 62]
_UHF = [0, 7, 13, 19, 20, 27, 33, 39, 45, 64]
_LBAND = [0, 6, 12, 20, 26, 32, 38, 43, 62]
_HIZ = [0, 6, 12, 18, 37]                       # RSP2 / RSPduo Hi-Z port, below 60 MHz
_DX = [(12, [0, 3, 6, 9, 12, 15, 24, 27, 30, 33, 36, 39, 42, 45, 48, 51, 54, 57, 60]),
       (50, [0, 3, 6, 9, 12, 15, 18, 24, 27, 30, 33, 36, 39, 42, 45, 48, 51, 54, 57, 60]),
       (60, [0, 3, 6, 9, 12, 20, 23, 26, 29, 32, 35, 38, 44, 47, 50, 53, 56, 59, 62, 65, 68, 71, 74, 77, 80]),
       (250, [0, 3, 6, 9, 12, 15, 24, 27, 30, 33, 36, 39, 42, 45, 48, 51, 54, 57, 60, 63, 66, 69, 72, 75, 78, 81, 84]),
       (420, [0, 3, 6, 9, 12, 15, 18, 24, 27, 30, 33, 36, 39, 42, 45, 48, 51, 54, 57, 60, 63, 66, 69, 72, 75, 78, 81, 84]),
       (1000, [0, 7, 10, 13, 16, 19, 22, 25, 31, 34, 37, 40, 43, 46, 49, 52, 55, 58, 61, 64, 67]),
       (2000, [0, 5, 8, 11, 14, 17, 20, 32, 35, 38, 41, 44, 47, 50, 53, 56, 59, 62, 65])]
_LNA_GR = {
    1: [(420, [0, 24, 19, 43]), (1000, [0, 7, 19, 26]), (2000, [0, 5, 19, 24])],
    255: [(60, _LOW), (420, _VHF), (1000, _UHF), (2000, _LBAND)],
    6: [(50, _LOW), (420, _VHF), (1000, _UHF), (2000, _LBAND)],
    2: [(420, [0, 10, 15, 21, 24, 34, 39, 45, 64]), (1000, [0, 7, 10, 17, 22, 41]), (2000, [0, 5, 21, 15, 15, 34])],
    3: [(60, _LOW), (420, _VHF), (1000, _UHF), (2000, _LBAND)],
    4: _DX, 7: _DX,
}
_IF_GR = (20, 59)       # sdrplay_api_NORMAL_MIN_GR .. MAX_BB_GR


def lna_table(hw_ver: int, freq_hz: float, hiz: bool = False) -> list[int]:
    if hiz and hw_ver in (2, 3) and freq_hz < 60e6:
        return _HIZ
    rows = _LNA_GR.get(hw_ver, _LNA_GR[255])
    return next((row for top, row in rows if freq_hz <= top * 1e6), rows[-1][1])


def max_reduction(hw_ver: int) -> int:
    """The most this model can reduce in any band: gain 0 dB on FalconSDR's scale."""
    return max(max(row) for _, row in _LNA_GR.get(hw_ver, _LNA_GR[255])) + _IF_GR[1]


def gain_range(hw_ver: int) -> tuple[float, float]:
    return 0.0, float(max_reduction(hw_ver) - _IF_GR[0])


def gain_split(hw_ver: int, freq_hz: float, gain_db: float, hiz: bool = False) -> tuple[int, int]:
    """FalconSDR's one gain number -> (LNA state, IF gain reduction gRdB). The API thinks in reductions; FalconSDR's
    scale is ``max_reduction - total reduction``, the same physical dB in every band, so SoftwareAgc (gotcha 27)
    steps work unchanged. The least LNA reduction that leaves the IF within 20..59 dB wins: the front end's noise
    figure is set before the IF stage."""
    target = max_reduction(hw_ver) - float(gain_db)
    states = sorted(enumerate(lna_table(hw_ver, freq_hz, hiz)), key=lambda s: s[1])
    for state, lna in states:
        if_gr = int(round(target - lna))
        if _IF_GR[0] <= if_gr <= _IF_GR[1]:
            return state, if_gr
    if target - states[0][1] < _IF_GR[0]:
        return states[0][0], _IF_GR[0]          # more gain asked than there is
    return states[-1][0], _IF_GR[1]             # less gain asked than there is


def bandwidth_khz(sample_rate: float) -> int:
    """The widest analog filter that still leaves room before Nyquist: wider aliases strong neighbours into the edges."""
    fits = [bw for bw in _BANDWIDTHS_KHZ if bw * 1000 <= 0.85 * sample_rate]
    return fits[-1] if fits else _BANDWIDTHS_KHZ[0]


# ----------------------------------------------------------------------- the library
_lib: ctypes.CDLL | None = None
_lib_tried = False
_lib_lock = threading.Lock()
_api_users = 0
_api_version: float | None = None


def _library_candidates() -> list[str]:
    out = [os.environ["FALCONSDR_LIBSDRPLAY"]] if os.environ.get("FALCONSDR_LIBSDRPLAY") else []
    if sys.platform == "win32":
        arch = "ARM64" if platform.machine().upper() == "ARM64" else "x64"
        try:
            import winreg
            for key in (r"SOFTWARE\SDRplay\Service\API", r"SOFTWARE\WOW6432Node\SDRplay\Service\API"):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                        base = winreg.QueryValueEx(k, "Install_Dir")[0]
                    out += [os.path.join(base, arch, "sdrplay_api.dll"), os.path.join(base, "x64", "sdrplay_api.dll")]
                except OSError:
                    continue
        except ImportError:
            pass
        out.append(rf"C:\Program Files\SDRplay\API\{arch}\sdrplay_api.dll")
    else:
        out += ["libsdrplay_api.so.3", "/usr/local/lib/libsdrplay_api.so.3", "libsdrplay_api.so",
                "/usr/local/lib/libsdrplay_api.so", "/opt/homebrew/lib/libsdrplay_api.so.3", "libsdrplay_api.dylib"]
    found = ctypes.util.find_library("sdrplay_api")
    if found:
        out.append(found)
    return out


def sdrplay_library() -> ctypes.CDLL | None:
    """The installed SDRplay API library with its prototypes declared, or None."""
    global _lib, _lib_tried
    with _lib_lock:
        if _lib_tried:
            return _lib
        _lib_tried = True
        for name in _library_candidates():
            try:
                lib = ctypes.CDLL(name)
            except OSError:
                continue
            try:
                P = c.POINTER
                sig = {
                    "sdrplay_api_Open": ([], _int), "sdrplay_api_Close": ([], _int),
                    "sdrplay_api_ApiVersion": ([P(_float)], _int),
                    "sdrplay_api_LockDeviceApi": ([], _int), "sdrplay_api_UnlockDeviceApi": ([], _int),
                    "sdrplay_api_GetDevices": ([P(_Device), P(_uint), _uint], _int),
                    "sdrplay_api_SelectDevice": ([P(_Device)], _int), "sdrplay_api_ReleaseDevice": ([P(_Device)], _int),
                    "sdrplay_api_GetErrorString": ([_int], c.c_char_p),
                    "sdrplay_api_GetDeviceParams": ([c.c_void_p, P(P(_DeviceParams))], _int),
                    "sdrplay_api_Init": ([c.c_void_p, P(_CallbackFns), c.c_void_p], _int),
                    "sdrplay_api_Uninit": ([c.c_void_p], _int),
                    "sdrplay_api_Update": ([c.c_void_p, _int, _uint, _uint], _int),
                    "sdrplay_api_SwapRspDuoActiveTuner": ([c.c_void_p, P(_int), _int], _int),
                }
                for fn_name, (args, res) in sig.items():
                    fn = getattr(lib, fn_name)
                    fn.argtypes, fn.restype = args, res
            except AttributeError:
                continue
            _lib = lib
            return lib
        return None


def _error(lib, code: int) -> str:
    try:
        return f"{lib.sdrplay_api_GetErrorString(code).decode()} ({code})"
    except Exception:  # noqa: BLE001
        return str(code)


def _api_acquire(lib) -> float:
    """sdrplay_api_Open() for the first user in this process (the spec: Open first, Close last, or the service is
    left in an unknown state), and the version check. Returns the API version or raises RuntimeError."""
    global _api_users, _api_version
    with _lib_lock:
        if _api_users == 0:
            rc = lib.sdrplay_api_Open()
            if rc != _SUCCESS:
                if rc == _SERVICE_NOT_RESPONDING:
                    raise RuntimeError("The SDRplay API service is not running - restart it (Windows: Services -> "
                                       "SDRplayAPIService; Linux: the sdrplay service) or reinstall the SDRplay API")
                raise RuntimeError(f"Could not open the SDRplay API: {_error(lib, rc)}")
            version = _float(0.0)
            lib.sdrplay_api_ApiVersion(c.byref(version))
            _api_version = round(version.value, 2)
            if _api_version < MIN_API_VERSION:
                lib.sdrplay_api_Close()
                raise RuntimeError(f"SDRplay API {_api_version:.2f} is installed - FalconSDR needs {MIN_API_VERSION:.2f} "
                                   f"or newer: install the current one from {API_DOWNLOAD_URL}")
        _api_users += 1
        return _api_version or 0.0


def _api_release(lib) -> None:
    global _api_users
    with _lib_lock:
        _api_users = max(0, _api_users - 1)
        if _api_users == 0:
            lib.sdrplay_api_Close()


def api_status() -> dict:
    """{"loads": bool, "version": "3.15" or "", "error": ""} - for the device list and the Listen page."""
    lib = sdrplay_library()
    if lib is None:
        return {"loads": False, "version": "", "error": ""}
    try:
        version = _api_acquire(lib)
    except RuntimeError as exc:
        return {"loads": True, "version": f"{_api_version:.2f}" if _api_version else "", "error": str(exc)}
    _api_release(lib)
    return {"loads": True, "version": f"{version:.2f}", "error": ""}


def _list_devices(lib) -> list[_Device]:
    """GetDevices under the device-API lock (the spec's order); the caller holds an _api_acquire()."""
    devices = (_Device * 16)()
    count = _uint(0)
    lib.sdrplay_api_LockDeviceApi()
    try:
        rc = lib.sdrplay_api_GetDevices(devices, c.byref(count), 16)
        if rc != _SUCCESS:
            raise RuntimeError(f"SDRplay: listing receivers failed: {_error(lib, rc)}")
        return [devices[i] for i in range(min(count.value, 16))]
    finally:
        lib.sdrplay_api_UnlockDeviceApi()


def enumerate_devices() -> list[dict]:
    lib = sdrplay_library()
    if lib is None:
        return []
    try:
        _api_acquire(lib)
    except RuntimeError:
        return []
    try:
        found = _list_devices(lib)
    except RuntimeError:
        return []
    finally:
        _api_release(lib)
    out = []
    for d in found:
        serial = d.SerNo.decode("ascii", "replace")
        out.append({"driver": SDRPLAY_NATIVE_DRIVER, "label": f"SDRplay {MODELS.get(d.hwVer, 'RSP')} (SDRplay API) SN {serial}",
                    "serial": serial})
    return out


# ----------------------------------------------------------------------- the backend
class SdrplayBackend(_NativeStreamBackend):
    label = SDRPLAY_NATIVE_DRIVER
    bytes_per_sample = 4        # int16 I + int16 Q, interleaved in the callback

    def __init__(self, device_args: str, sample_rate: float, gain_mode: str, gain_db: float, bias_tee: bool = False,
                 antenna: str = ""):
        lib = sdrplay_library()
        if lib is None:
            raise RuntimeError(f"The SDRplay API is not installed - install it from {API_DOWNLOAD_URL} (free, once; "
                               "it brings the driver and a service), then start scanning again")
        lo, hi = SAMPLE_RATES_HZ
        if not lo <= sample_rate <= hi:
            raise RuntimeError(f"SDRplay receivers capture 2 to 10 MS/s here - set the capture width to 2..10 MHz "
                               f"(now {sample_rate / 1e6:.2f}; 6 MHz is a good start)")
        super().__init__(sample_rate)
        self._lib = lib
        self._open = False
        self._initialised = False
        self.overloads = 0
        self.retune_timeouts = 0
        self._await = ""                # "rf"/"gr": drop blocks until the stream says the change is in them
        self._await_until = 0.0
        self._freq = 0.0
        self._gain = (gain_mode, float(gain_db))
        SoapyBackend._wait_for_reopen_cooldown()
        _api_acquire(lib)
        self._api_held = True
        try:
            self._select(device_args, antenna)
            self._configure(sample_rate, antenna)
            self._cbs = _CallbackFns(_STREAM_CB(self._on_stream), _STREAM_CB(self._on_stream_b), _EVENT_CB(self._on_event))
            self._cb = self._cbs                                # keeps the ctypes callbacks alive
            self._check(lib.sdrplay_api_Init(self._dev.dev, c.byref(self._cbs), None), "Init")
            self._initialised = True
            if bias_tee:                                        # gotcha 47: the last step of a successful open
                self._set_bias_tee(True)
        except Exception:
            self.close()
            raise

    # -- opening ---------------------------------------------------------------------------------------
    def _check(self, rc: int, what: str) -> None:
        if rc != _SUCCESS:
            raise RuntimeError(f"SDRplay {what} failed: {_error(self._lib, rc)}")

    def _select(self, device_args: str, antenna: str) -> None:
        args = {k.strip(): v.strip() for k, v in (kv.split("=", 1) for kv in (device_args or "").split(",") if "=" in kv)}
        devices = _list_devices(self._lib)
        serial = args.get("serial", "")
        pick = next((d for d in devices if not serial or d.SerNo.decode("ascii", "replace") == serial), None)
        if pick is None:
            raise RuntimeError(f"SDRplay receiver {serial} is not connected (or another program uses it)" if serial
                               else "No SDRplay receiver found (connected? used by SDRconnect or SDR++?)")
        self.hw_ver = pick.hwVer
        self.model = MODELS.get(pick.hwVer, f"RSP (hw {pick.hwVer})")
        if pick.hwVer == 3:                                     # RSPduo: one tuner, like any other RSP
            pick.rspDuoMode = _DUO_SINGLE_TUNER
            pick.tuner = _TUNER_B if antenna == ANTENNAS[3][1] else _TUNER_A
        self._dev = pick
        self._lib.sdrplay_api_LockDeviceApi()
        try:
            self._check(self._lib.sdrplay_api_SelectDevice(c.byref(self._dev)), "SelectDevice")
        finally:
            self._lib.sdrplay_api_UnlockDeviceApi()
        self._open = True
        self.tuner = self._dev.tuner if pick.hwVer == 3 else _TUNER_A
        self.antennas = ANTENNAS.get(pick.hwVer, ())
        self.gain_range_db = gain_range(pick.hwVer)

    def _configure(self, sample_rate: float, antenna: str) -> None:
        params = c.POINTER(_DeviceParams)()
        self._check(self._lib.sdrplay_api_GetDeviceParams(self._dev.dev, c.byref(params)), "GetDeviceParams")
        self._params = params.contents
        self._ch = (self._params.rxChannelB if self.tuner == _TUNER_B else self._params.rxChannelA).contents
        if self._params.devParams:
            self._params.devParams.contents.fsFreq.fsHz = sample_rate
        tp = self._ch.tunerParams
        tp.bwType, tp.ifType, tp.loMode = bandwidth_khz(sample_rate), _IF_ZERO, _LO_AUTO
        tp.rfFreq.rfHz = 125e6
        self._freq = 125e6
        ctrl = self._ch.ctrlParams
        ctrl.decimation.enable = 0
        ctrl.dcOffset.DCenable, ctrl.dcOffset.IQenable = 1, 1
        self._apply_antenna(antenna, live=False)
        self._apply_gain(live=False)
        self._set_bias_fields(False)

    # -- the stream ------------------------------------------------------------------------------------
    def _on_stream(self, xi, xq, params, num_samples, reset, _ctx) -> None:
        try:
            if self._await:
                p = params.contents
                changed = p.rfChanged if self._await == "rf" else p.grChanged
                if changed or time.monotonic() > self._await_until:
                    if not changed:
                        self.retune_timeouts += 1
                    self._await = ""    # this block may hold both frequencies: it goes too
                return
            n = int(num_samples)
            if n <= 0:
                return
            iq = np.empty(2 * n, dtype=np.int16)
            iq[0::2] = np.ctypeslib.as_array(xi, (n,))
            iq[1::2] = np.ctypeslib.as_array(xq, (n,))
            self._stream.push(iq.tobytes(), time.monotonic())
        except Exception as exc:  # noqa: BLE001 - never let an exception unwind into C
            self._stream.fail(f"SDRplay stream callback failed: {exc}")

    def _on_stream_b(self, xi, xq, params, num_samples, reset, ctx) -> None:
        if self.tuner == _TUNER_B:           # RSPduo on tuner 2, single tuner mode
            self._on_stream(xi, xq, params, num_samples, reset, ctx)

    def _on_event(self, event_id, tuner, params, _ctx) -> None:
        try:
            if event_id == _EV_POWER_OVERLOAD:
                if params and params.contents.powerOverloadChangeType == _OVERLOAD_DETECTED:
                    self.overloads += 1
                # The spec: every overload message must be acknowledged, or the API stops reporting gain changes.
                self._lib.sdrplay_api_Update(self._dev.dev, tuner, _UPD_OVERLOAD_ACK, 0)
            elif event_id in (_EV_DEVICE_REMOVED, _EV_DEVICE_FAILURE):
                self._stream.fail("The SDRplay receiver was unplugged or failed")
        except Exception:  # noqa: BLE001
            pass

    def _expect(self, what: str, wait_s: float) -> None:
        """Blocks from before ``what`` changed are thrown away until the stream marks the change (rfChanged/grChanged)
        - exact, unlike counting transfers by the clock - with a time limit in case the flag never comes."""
        self._await_until = time.monotonic() + wait_s
        self._await = what
        self._stream.clear()

    def extra_health(self) -> dict:
        """Shown in Settings -> Receiver health and the report (AcquisitionThread.health)."""
        return {"model": self.model, "api": f"{_api_version:.2f}" if _api_version else "", "overloads": self.overloads,
                "retune_timeouts": self.retune_timeouts}

    @staticmethod
    def to_iq(raw: np.ndarray) -> np.ndarray:
        """int16 I/Q -> CF32, full scale = 32768."""
        return (raw.view(np.int16).astype(np.float32) * np.float32(1 / 32768)).view(np.complex64)

    # -- controls ----------------------------------------------------------------------------------------
    def _update(self, reason: int, ext: int = 0) -> None:
        if self._initialised:
            self._check(self._lib.sdrplay_api_Update(self._dev.dev, self.tuner, reason, ext), "Update")

    def set_frequency(self, freq_hz: float) -> None:
        if freq_hz == self._freq:
            return
        old_table = lna_table(self.hw_ver, self._freq, self._hiz)
        self._freq = float(freq_hz)
        self._ch.tunerParams.rfFreq.rfHz = self._freq
        reason = _UPD_TUNER_FRF
        if lna_table(self.hw_ver, self._freq, self._hiz) is not old_table:     # another band: the same dB is another split
            self._apply_gain(live=False)
            reason |= _UPD_TUNER_GR
        self._expect("rf", _RETUNE_WAIT_S)
        self._update(reason)

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        self._gain = (gain_mode, float(gain_db))
        self._apply_gain(live=True)

    def _apply_gain(self, live: bool) -> None:
        mode, db = self._gain
        state, if_gr = gain_split(self.hw_ver, self._freq, db, self._hiz)
        g = self._ch.tunerParams.gain
        g.LNAstate, g.gRdB = state, if_gr
        agc = self._ch.ctrlParams.agc
        agc.enable = _AGC_CTRL_EN if mode == "auto" else _AGC_DISABLE     # the RSP's own AGC moves the IF stage only
        agc.setPoint_dBfs = -30
        if live:
            self._expect("gr", _GAIN_WAIT_S)
            self._update(_UPD_TUNER_GR | _UPD_AGC)

    @property
    def _hiz(self) -> bool:
        return getattr(self, "_antenna", "") in ("Hi-Z", "Tuner 1 Hi-Z")

    def set_antenna(self, antenna: str) -> None:
        self._apply_antenna(antenna, live=True)

    def _apply_antenna(self, antenna: str, live: bool) -> None:
        names = ANTENNAS.get(self.hw_ver, ())
        if not antenna or antenna not in names:
            self._antenna = names[0] if names else ""
            if antenna and live:    # at open an unknown name (another driver's) means the default port, not a reconnect loop
                raise RuntimeError(f"{self.model} has no antenna port '{antenna}' (it has: {', '.join(names) or 'one'})")
            return
        self._antenna = antenna
        if self.hw_ver == 2:
            t = self._ch.rsp2TunerParams
            t.antennaSel = _RSP2_ANTENNA_A if antenna == "Antenna A" else _RSP2_ANTENNA_B
            t.amPortSel = _AMPORT_1 if antenna == "Hi-Z" else _AMPORT_2
            self._update(_UPD_RSP2_ANTENNA | _UPD_RSP2_AMPORT)
        elif self.hw_ver in (4, 7):
            self._params.devParams.contents.rspDxParams.antennaSel = names.index(antenna)
            self._update(0, _EXT_DX_ANTENNA)
        elif self.hw_ver == 3:
            want = _TUNER_B if antenna == names[1] else _TUNER_A
            port = _AMPORT_1 if antenna == names[2] else _AMPORT_2
            if live and want != self.tuner:
                tuner = _int(self.tuner)
                self._check(self._lib.sdrplay_api_SwapRspDuoActiveTuner(self._dev.dev, c.byref(tuner), port), "SwapRspDuoActiveTuner")
                self.tuner = tuner.value
                self._ch = (self._params.rxChannelB if self.tuner == _TUNER_B else self._params.rxChannelA).contents
            elif want == _TUNER_A:
                self._ch.rspDuoTunerParams.tuner1AmPortSel = port
                self._update(_UPD_DUO_AMPORT)
        if live:
            self._expect("rf", _RETUNE_WAIT_S)

    def _set_bias_fields(self, on: bool) -> tuple[int, int]:
        v = 1 if on else 0
        if self.hw_ver in (255, 6):
            self._ch.rsp1aTunerParams.biasTEnable = v
            return _UPD_RSP1A_BIAST, 0
        if self.hw_ver == 2:
            self._ch.rsp2TunerParams.biasTEnable = v
            return _UPD_RSP2_BIAST, 0
        if self.hw_ver == 3:
            self._ch.rspDuoTunerParams.biasTEnable = v
            return _UPD_DUO_BIAST, 0
        if self.hw_ver in (4, 7):
            self._params.devParams.contents.rspDxParams.biasTEnable = v
            return 0, _EXT_DX_BIAST
        return 0, 0

    def _set_bias_tee(self, on: bool) -> None:
        if on and self.hw_ver == 1:
            raise RuntimeError("The RSP1 has no Bias-T")
        reason, ext = self._set_bias_fields(on)
        if reason or ext:
            self._update(reason, ext)
        self.bias_tee = on

    # -- closing -----------------------------------------------------------------------------------------
    def close(self) -> None:
        lib = self._lib
        if self._initialised:
            for step in ((lambda: self._set_bias_tee(False)) if self.bias_tee else None,
                         lambda: lib.sdrplay_api_Uninit(self._dev.dev)):
                if step is None:
                    continue
                try:
                    step()
                except Exception:  # noqa: BLE001 - closing goes on: a held device blocks every other program
                    pass
            self._initialised = False
        if self._open:
            try:
                lib.sdrplay_api_ReleaseDevice(c.byref(self._dev))
            except Exception:  # noqa: BLE001
                pass
            self._open = False
            SoapyBackend._record_close_time()
        if getattr(self, "_api_held", False):
            self._api_held = False
            _api_release(lib)
        self._cb = None
        self._stream.clear()
