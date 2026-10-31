"""``python main.py --probe-sdr [driver]``: measures what the backends can only assume, on a real receiver.

Stop the scanner first (the receiver can be opened once). Prints a short report and one ``RESULT {...}``
line to paste into an issue. What it checks: the gain steps (level of the noise floor per setting), the DC
spike at the window centre, how many blocks after a retune still hold the previous window (they read as
ghost peaks), what a retune costs, and whether samples are lost.
"""
from __future__ import annotations

import json
import platform
import sys
import time

import numpy as np

from . import __version__
from .config import AppConfig
from .sdr_backend import HAVE_RTLSDR_NATIVE, HAVE_SOAPY, enumerate_devices, open_real_backend

_NFFT = 4096


def _spectrum(iq: np.ndarray) -> np.ndarray:
    seg = iq[: _NFFT * (len(iq) // _NFFT)].reshape(-1, _NFFT) * np.hanning(_NFFT)
    return np.fft.fftshift(10 * np.log10(np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0) / _NFFT + 1e-20))


def _norm(spec: np.ndarray) -> np.ndarray:
    v = spec - spec.mean()
    return v / (np.linalg.norm(v) + 1e-12)


def run(driver: str = "") -> int:
    cfg = AppConfig.load().device
    devices = enumerate_devices()
    if driver:
        match = [d for d in devices if d.get("driver") == driver]
        if not match and driver not in ("auto", "rtlsdr", "rtlsdr_native", "hackrf", "hackrf_native", "airspy", "airspy_native"):
            print(f"No such driver: {driver}. Available: {sorted({d.get('driver') for d in devices})}")
            return 2
        pick = match[0] if match else {"driver": driver}
    else:
        pick = {"driver": cfg.driver, "args": cfg.device_args}
    drv = pick["driver"]
    args = pick.get("args") or ",".join(f"{k}={v}" for k, v in pick.items() if k != "driver")
    rate = cfg.sample_rate_hz
    print(f"FalconSDR {__version__} | {platform.platform()} | Python {platform.python_version()} | SoapySDR: {HAVE_SOAPY} | pyrtlsdr: {HAVE_RTLSDR_NATIVE}")
    print(f"Devices found: {[d.get('label') or d.get('driver') for d in devices]}")
    print(f"Probing driver {drv!r} at {rate / 1e6:g} MS/s (stop the scanner first)")
    try:
        be = open_real_backend(drv, args, rate, "manual", 30.0)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open it: {exc}")
        return 1
    chunk = int(rate * 0.03)
    result: dict = {"driver": drv, "backend": type(be).__name__, "rate_hz": rate, "version": __version__}
    try:
        lo, hi = be.gain_range_db
        base = 98e6 if rate < 6e6 else 100e6
        be.set_frequency(base)
        for _ in range(20):
            be.read(chunk)

        # 1. gain steps: noise floor per setting
        floors = {}
        for frac in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85):
            g = lo + (hi - lo) * frac
            be.set_gain("manual", g)
            for _ in range(12):
                be.read(chunk)
            floors[round(g, 1)] = round(float(np.median(_spectrum(np.concatenate([be.read(chunk) for _ in range(4)])))), 1)
        print("Noise floor (dBFS) per requested gain:", floors)
        result["floor_by_gain"] = floors
        be.set_gain("manual", lo + (hi - lo) * 0.4)
        for _ in range(12):
            be.read(chunk)

        # 2. DC spike
        spec = np.mean([_spectrum(be.read(chunk)) for _ in range(20)], axis=0)
        mid = _NFFT // 2
        dc = float(spec[mid - 1:mid + 2].max() - np.median(spec))
        print(f"DC spike at the window centre: {dc:+.1f} dB over the median floor")
        result["dc_spike_db"] = round(dc, 1)

        # 3. stale blocks after a retune: classify each block by which window's spectrum it resembles
        c1, c2 = base, base + max(4e6, 2 * rate)

        def settle(c):
            be.set_frequency(c)
            for _ in range(30):
                be.read(chunk)
        settle(c1)
        ref1 = _norm(np.mean([_spectrum(be.read(chunk)) for _ in range(20)], axis=0))
        settle(c2)
        ref2 = _norm(np.mean([_spectrum(be.read(chunk)) for _ in range(20)], axis=0))
        if float(ref1 @ ref2) > 0.9:
            print("Retune check: inconclusive - both windows look alike (no stations to tell them apart)")
        else:
            stale, cost, first_clean = [], [], []
            for _ in range(15):
                settle(c1)
                t0 = time.monotonic()
                be.set_frequency(c2)
                cost.append((time.monotonic() - t0) * 1000)
                n = 0
                while n < 20:
                    block = _norm(_spectrum(be.read(chunk)))
                    if float(block @ ref1) <= float(block @ ref2):
                        break
                    n += 1
                stale.append(n)
                first_clean.append((time.monotonic() - t0) * 1000)
            print(f"Retune: set_frequency() {np.mean(cost):.0f} ms; blocks of the OLD window afterwards: mean {np.mean(stale):.2f}, max {max(stale)}; "
                  f"time to the first clean block {np.mean(first_clean):.0f} ms")
            result.update(retune_ms=round(float(np.mean(cost)), 1), stale_blocks_mean=round(float(np.mean(stale)), 2),
                          stale_blocks_max=int(max(stale)), first_clean_ms=round(float(np.mean(first_clean)), 1))

        # 4. throughput: 8 s of back-to-back reads
        settle(c1)
        lost0 = be.stream_stats()[0] if hasattr(be, "stream_stats") else 0.0
        reads, t_start = [], time.monotonic()
        while time.monotonic() - t_start < 8:
            t = time.monotonic()
            be.read(chunk)
            reads.append((time.monotonic() - t) * 1000)
        wall = time.monotonic() - t_start
        got = len(reads) * chunk / rate
        lost = (be.stream_stats()[0] - lost0) if hasattr(be, "stream_stats") else None
        print(f"Throughput: {got:.2f} s of IQ in {wall:.2f} s of wall time; read() max {max(reads):.0f} ms (chunk {chunk / rate * 1000:.0f} ms)"
              + (f"; reported lost {lost:.3f} s" if lost is not None else ""))
        result.update(iq_seconds=round(got, 2), wall_seconds=round(wall, 2), read_max_ms=round(max(reads), 1),
                      lost_seconds=None if lost is None else round(lost, 3))
    except Exception as exc:  # noqa: BLE001
        print(f"Probe stopped: {exc}")
        result["error"] = str(exc)
        return 1
    finally:
        be.close()
    print("RESULT " + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else ""))
