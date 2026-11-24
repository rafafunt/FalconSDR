"""``python main.py --probe-sdr [driver]``: measures what the backends can only assume, on a real receiver.

Stop the scanner first (the receiver can be opened once). Prints a short report and one ``RESULT {...}``
line to paste into an issue. What it checks: the gain steps (level of the noise floor per setting), the DC
spike at the window centre, how many blocks after a retune still hold the previous window (they read as
ghost peaks), what a retune costs, and whether samples are lost.
"""
from __future__ import annotations

import json
import os
import re
import platform
import subprocess
import sys
import threading
import time
from collections import deque

import numpy as np

from . import __version__
from .config import AppConfig
from .sdr_backend import HAVE_RTLSDR_NATIVE, HAVE_SOAPY, enumerate_devices, open_real_backend

_NFFT = 4096
_TIMEOUT_S = 180.0      # a full probe takes about a minute; a driver hung in a read must not keep the receiver forever
_MAX_LINES = 400
_STEP = re.compile(r"^Step (\d+)/(\d+)")
_lock = threading.Lock()
_state: dict = {"running": False, "lines": [], "code": None, "step": 0, "steps": 0, "started": 0.0}


def state() -> dict:
    """The UI's receiver test (Settings): running, its report so far, which step, the exit code when done."""
    with _lock:
        out = {**_state, "lines": list(_state["lines"])}
    started = out.pop("started")
    out["elapsed_s"] = round(time.monotonic() - started) if out["running"] else None
    return out


def start_in_background() -> None:
    """Runs ``main.py --probe-sdr`` as a child: a crash or a hang in a driver costs the child, not the server
    (gotcha 55). The caller makes sure the scanner and the add-ons have let go of the receiver."""
    with _lock:
        if _state["running"]:
            from .i18n import tr
            raise RuntimeError(tr("The receiver test is already running."))
        _state.update(running=True, lines=[], code=None, step=0, steps=0, started=time.monotonic())
    threading.Thread(target=_run_child, daemon=True, name="sdr-probe").start()


def _run_child() -> None:
    from .sdr_native import self_command
    code = -1
    # The report is stdout. Drivers and libraries write to stderr (librtlsdr's "Allocating 15 zero-copy buffers" at every
    # retune, UHD, ALSA, avahi): noise next to the numbers, so only its tail is kept - and shown when the test failed.
    driver_lines: deque[str] = deque(maxlen=30)
    try:
        proc = subprocess.Popen(self_command("--probe-sdr"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONUNBUFFERED": "1"},     # lines arrive as they are printed
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        watchdog = threading.Timer(_TIMEOUT_S, proc.kill)
        watchdog.start()
        errors = threading.Thread(target=lambda: driver_lines.extend(line.rstrip() for line in proc.stderr), daemon=True)
        errors.start()
        try:
            for line in proc.stdout:
                step = _STEP.match(line)
                with _lock:
                    if step:
                        _state.update(step=int(step[1]), steps=int(step[2]))
                    elif len(_state["lines"]) < _MAX_LINES:
                        _state["lines"].append(line.rstrip())
            code = proc.wait()
            errors.join(5)
        finally:
            watchdog.cancel()
    except OSError as exc:
        driver_lines.append(f"Could not start the test: {exc}")
    finally:
        with _lock:
            if code != 0 and driver_lines:
                _state["lines"] += ["", "Driver messages (last lines):", *[line for line in driver_lines if line]]
            _state.update(running=False, code=code)


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
        if not match and driver not in ("auto", "rtlsdr", "rtlsdr_native", "rtl_tcp", "hackrf", "hackrf_native", "airspy", "airspy_native", "sdrplay", "sdrplay_native"):
            print(f"No such driver: {driver}. Available: {sorted({d.get('driver') for d in devices})}")
            return 2
        pick = match[0] if match else {"driver": driver}
        if driver == "rtl_tcp":
            pick["args"] = cfg.device_args if cfg.driver == "rtl_tcp" else ""  # the address saved on the Listen page
    else:
        pick = {"driver": cfg.driver, "args": cfg.device_args}
    drv = pick["driver"]
    args = pick.get("args") or ",".join(f"{k}={v}" for k, v in pick.items() if k != "driver")
    rate = cfg.sample_rate_hz
    print(f"FalconSDR {__version__} | {platform.platform()} | Python {platform.python_version()} | SoapySDR: {HAVE_SOAPY} | pyrtlsdr: {HAVE_RTLSDR_NATIVE}")
    print(f"Devices found: {[d.get('label') or d.get('driver') for d in devices]}")
    print(f"Settings: gain {cfg.gain_mode} {cfg.gain_db:g} dB, continuous reading {'on' if cfg.async_read else 'off'}, "
          f"bias-T {'on' if cfg.bias_tee else 'off'}, extra receivers {len(cfg.extra_devices)}")
    print(f"Probing driver {drv!r} at {rate / 1e6:g} MS/s (stop the scanner first)")
    try:
        be = open_real_backend(drv, args, rate, "manual", 30.0)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open it: {exc}")
        return 1
    chunk = int(rate * 0.03)
    result: dict = {"driver": drv, "backend": type(be).__name__, "rate_hz": rate, "version": __version__}
    try:
        print("Step 1/4: gain steps", flush=True)     # the UI shows these as progress
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
        print("Step 2/4: DC spike", flush=True)
        spec = np.mean([_spectrum(be.read(chunk)) for _ in range(20)], axis=0)
        mid = _NFFT // 2
        dc = float(spec[mid - 1:mid + 2].max() - np.median(spec))
        print(f"DC spike at the window centre: {dc:+.1f} dB over the median floor")
        result["dc_spike_db"] = round(dc, 1)

        # 3. stale blocks after a retune: classify each block by which window's spectrum it resembles
        print("Step 3/4: retuning", flush=True)
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
        print("Step 4/4: lost samples", flush=True)
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
