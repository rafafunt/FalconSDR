"""Paths and constants of the recording library.

The library uses AppConfig.recording.output_dir; ``FALCONSDR_DIR`` is only
the default when a function gets no ``root``.
"""
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import DEFAULT_RECORDINGS_DIR

FALCONSDR_DIR = Path(DEFAULT_RECORDINGS_DIR)

UTC_TZ = ZoneInfo("UTC")

# Application data directory (quality cache) - separate from the recordings
# directory, so it doesn't clutter the scanner's own folder.
APP_DATA_DIR = Path.home() / ".local" / "share" / "FalconSDR"
_OLD_APP_DATA_DIR = Path.home() / ".local" / "share" / "atc_browser"  # name until 2026-10
if not APP_DATA_DIR.exists() and _OLD_APP_DATA_DIR.is_dir():
    import shutil
    shutil.copytree(_OLD_APP_DATA_DIR, APP_DATA_DIR)  # copy: the old folder stays as a backup
QUALITY_CACHE_FILE = APP_DATA_DIR / "quality_cache.json"
TRANSCRIPTION_CACHE_FILE = APP_DATA_DIR / "transcription_cache.json"
FAVORITES_FILE = APP_DATA_DIR / "favorites.json"

# Date subfolder name format: YYYY-MM-DD
DATE_DIR_FORMAT = "%Y-%m-%d"

# Filename: 2026-07-23_19-31-01-123456_134474.wav - local time, optional
# microseconds (older files have none), frequency in kHz. A manual recording
# ends in "_session" instead (gotcha 19). .wav or .mp3.
FILENAME_REGEX = r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})(?:-(\d{6}))?_(?:(\d+)|session)\.(?:wav|mp3)$"

APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
