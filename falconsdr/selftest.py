"""`main.py --self-test`: proves a build carries everything a machine with
nothing installed needs - run by the packaging scripts on the *built* app,
so a dependency PyInstaller silently dropped (AGENTS.md gotcha 35: tzdata,
librtlsdr) fails the build instead of a user's first launch."""
from __future__ import annotations

import sys
from pathlib import Path


def _checks():
    def numpy_scipy():
        import numpy
        import scipy.signal
        return f"numpy {numpy.__version__}, scipy.signal ok ({scipy.signal.resample_poly.__name__})"

    def timezones():
        from zoneinfo import ZoneInfo
        ZoneInfo("Europe/Warsaw")
        return "Europe/Warsaw ok"

    def rtlsdr_library():
        from falconsdr import sdr_backend
        if not sdr_backend.HAVE_RTLSDR_NATIVE:
            raise RuntimeError("pyrtlsdr/librtlsdr could not be loaded")
        import rtlsdr
        return f"librtlsdr loaded ({Path(rtlsdr.__file__).parent.name})"

    def web_ui():
        from falconsdr.web.server import STATIC_DIR
        missing = [f for f in ("index.html", "app.css", "icon.svg", "js/main.js") if not (STATIC_DIR / f).is_file()]
        if missing:
            raise RuntimeError(f"missing in {STATIC_DIR}: {missing}")
        return "static files ok"

    def translations():
        from falconsdr import i18n
        if "pl" not in i18n.available_languages():
            raise RuntimeError("pl.po not found")
        return "pl ok"

    def band_presets():
        from falconsdr import military_presets
        n = len(military_presets.get_all_presets())
        if not n:
            raise RuntimeError("no band presets")
        return f"{n} presets"

    def demodulator():
        import numpy as np
        from falconsdr.dsp import Demodulator
        from falconsdr.models import Modulation
        iq = (np.random.default_rng(0).standard_normal(48000) * 0.01).astype(np.complex64)
        Demodulator(2_400_000).demod(iq, Modulation.AM, 0.0)
        return "AM demod ok"

    def mp3():
        import tempfile
        import wave
        import numpy as np
        from falconsdr.audio_transcode import ffmpeg_exe, transcode_to_mp3
        from falconsdr.playback.audio_analysis import analyze_wav
        exe = ffmpeg_exe()
        if exe is None:
            raise RuntimeError("no ffmpeg (system or imageio-ffmpeg)")
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "t.wav"
            tone = (np.sin(np.arange(12500) * 2 * np.pi * 1000 / 12500) * 8000).astype("<i2")
            with wave.open(str(wav_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(12500)
                w.writeframes(tone.tobytes())
            mp3_path = transcode_to_mp3(wav_path)
            if mp3_path is None:
                raise RuntimeError(f"{exe} could not encode MP3")
            decoded = analyze_wav(mp3_path)
            if not decoded.readable or decoded.duration_sec < 0.5:
                raise RuntimeError("MP3 decoded to nothing")
        return f"encode + decode ok ({Path(exe).name})"

    def addon_installer():
        if not getattr(sys, "frozen", False):
            return "not a packaged build - pip of the running Python is used"
        from pip import __version__
        from pip._internal.cli.main import main  # noqa: F401 - what `--pip` runs
        from pip._vendor import distlib
        launcher = Path(distlib.__file__).parent / "t64.exe"
        if sys.platform == "win32" and not launcher.is_file():
            raise RuntimeError(f"pip's script launchers missing ({launcher})")
        if sys.platform == "win32" and not (Path(sys.executable).parent / "_internal" / "python3.dll").is_file():
            raise RuntimeError("python3.dll missing - abi3 add-ons (PyAV) can't load")
        return f"bundled pip {__version__}"

    return [numpy_scipy, timezones, rtlsdr_library, web_ui, translations, band_presets, demodulator, mp3, addon_installer]


def run() -> int:
    failed = 0
    for check in _checks():
        try:
            print(f"OK    {check.__name__}: {check()}", flush=True)
        except Exception as exc:  # report every failure, not just the first
            failed += 1
            print(f"FAIL  {check.__name__}: {type(exc).__name__}: {exc}", flush=True)
    print("SELF-TEST " + ("PASSED" if not failed else f"FAILED ({failed})"), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
