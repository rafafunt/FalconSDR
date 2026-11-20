"""Optional WAV -> MP3 transcode, run only after a recording has been fully
written and closed (see recorder.py / scanner_engine.py's finish-line call
sites) - never on the real-time recording path itself, per this project's own
long-standing rule (a transcode is a whole-file operation; doing it per-chunk
would be both pointless and slow).

``ffmpeg``: a system one on PATH wins, otherwise the binary shipped in the
``imageio-ffmpeg`` pip package (requirements.txt) - so MP3 also works in the
packaged builds with nothing installed. Still degrades gracefully: with no
ffmpeg at all, the WAV is simply left in place rather than the recording
being lost or the scanner failing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


# A console program started from the windowed Windows build would flash a
# console window per call; 0 (no flags) everywhere else.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def system_env() -> dict | None:
    """Environment for running a *system* program (ffmpeg, nvidia-smi). A
    frozen Linux/macOS build points LD_LIBRARY_PATH/DYLD_LIBRARY_PATH at its
    own bundled libraries; a system ffmpeg loading those fails."""
    if not getattr(sys, "frozen", False):
        return None
    env = dict(os.environ)
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        orig = env.pop(var + "_ORIG", None)  # PyInstaller keeps the user's own value here
        if orig is None:
            env.pop(var, None)
        else:
            env[var] = orig
    return env


def ffmpeg_exe() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # not installed, or no binary for this platform
        return None


def ffmpeg_available() -> bool:
    return ffmpeg_exe() is not None


def transcode_to_mp3(wav_path: Path, bitrate_kbps: int = 96) -> Path | None:
    """Transcodes ``wav_path`` to an .mp3 alongside it and deletes the
    original WAV on success. Returns the new path, or ``None`` (leaving the
    WAV untouched) if ffmpeg is missing or the transcode fails for any
    reason - a failed/missing transcode should never lose the recording
    itself."""
    ffmpeg = ffmpeg_exe()
    if ffmpeg is None:
        return None
    mp3_path = wav_path.with_suffix(".mp3")
    # Written under a non-.mp3 name first and renamed when complete, so the
    # library (which lists *.wav/*.mp3) never shows a half-written file.
    partial = wav_path.with_suffix(".mp3.part")
    try:
        subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(wav_path),
                "-codec:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k",
                "-f", "mp3", str(partial),
            ],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
            creationflags=NO_WINDOW,
            env=system_env(),
        )
        partial.replace(mp3_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        wav_path.unlink()
    except OSError:
        pass
    return mp3_path
