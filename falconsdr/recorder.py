"""WAV writer: audio of the transmissions the scanner stops on, one file
each; also the continuous manual recording (``single_file``).
"""
from __future__ import annotations

import datetime as dt
import wave
from pathlib import Path
from typing import List

import numpy as np

from .config import RecordingConfig


class Recorder:
    def __init__(self, config: RecordingConfig, audio_rate: int, single_file: bool = False):
        """``single_file`` is True only for the manual recording (gotcha 13);
        otherwise each transmission gets its own file in begin_transmission()."""
        self.config = config
        self.audio_rate = audio_rate
        self._single_file = single_file
        self._wav: wave.Wave_write | None = None
        self._current_path: Path | None = None
        self._session_active = False
        # Current file got real (not squelch-substituted) audio.
        self._has_real_audio = False
        # Squelched silence waits here: written if real audio follows, dropped
        # if the file closes first (gotcha 6).
        self._pending_silence: List[np.ndarray] = []

    @property
    def current_path(self) -> str:
        return str(self._current_path) if self._current_path else ""

    @property
    def is_writing(self) -> bool:
        return self._wav is not None

    def start_session(self) -> None:
        """Opens the file right away only for ``single_file``."""
        self._session_active = True
        Path(self.config.output_dir).mkdir(parents=True, exist_ok=True)
        if self._single_file:
            self._open_file(self._single_file_path())

    def stop_session(self) -> None:
        """Called when the scanner is stopped entirely."""
        self._close_file()
        self._session_active = False

    def begin_transmission(self, freq_hz: int) -> None:
        """Opens a new per-transmission file (not used with ``single_file``)."""
        if not self._session_active:
            return
        self._open_file(self._split_file_path(freq_hz))

    def write_audio(self, audio: np.ndarray, real: bool = True) -> None:
        """Writes mono ``audio``. ``real=False`` (squelched silence) is held
        back: flushed before the next real audio - a pause in the middle -
        or dropped when the file closes - no dead air at the end."""
        if self._wav is None:
            return
        if not real:
            self._pending_silence.append(audio)
            return
        if self._pending_silence:
            for chunk in self._pending_silence:
                self._write_raw(chunk)
            self._pending_silence = []
        self._write_raw(audio)
        self._has_real_audio = True

    def end_transmission(self) -> Path | None:
        """Closes the per-transmission file and returns its path - or deletes
        it and returns None if it never got real audio (a short spike that
        was squelched throughout would otherwise leave a silent WAV)."""
        path = self._current_path
        had_real_audio = self._has_real_audio
        self._close_file()
        if path is not None and not had_real_audio:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return path

    def abort(self) -> None:
        """Gives up on the current file after a write error (disk full, drive
        gone): a WAV whose header can't be finished is unplayable anyway, and
        deleting it frees space. Later write_audio() calls are no-ops."""
        path = self._current_path
        self._close_file()
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    # -- internals ---------------------------------------------------
    def _single_file_path(self) -> Path:
        # Straight into the date folder - organize() would move a loose file
        # while it's still open.
        now = dt.datetime.now()
        return Path(self.config.output_dir) / now.strftime("%Y-%m-%d") / f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_session.wav"

    @staticmethod
    def rename_session_file_with_frequency(path: Path, freq_hz: int) -> Path | None:
        """Renames a closed manual "..._session" file to "..._<freq_khz>",
        like auto-recordings (gotcha 13). Returns None, keeping the old name,
        if there's no marker or the rename fails - never a recording failure."""
        khz = round(freq_hz / 1000)
        new_name = path.name.replace("_session", f"_{khz}")
        if new_name == path.name:
            return None
        new_path = path.with_name(new_name)
        try:
            path.rename(new_path)
        except OSError:
            return None
        return new_path

    def _split_file_path(self, freq_hz: int) -> Path:
        now = dt.datetime.now()
        # Microseconds: two transmissions on one frequency within the same
        # second used to share a name, the second overwriting the first.
        stamp = now.strftime("%Y-%m-%d_%H-%M-%S-") + f"{now.microsecond:06d}"
        khz = round(freq_hz / 1000)
        # Straight into the date folder the library expects (organizer.py).
        date_dir = Path(self.config.output_dir) / now.strftime("%Y-%m-%d")
        return date_dir / f"{stamp}_{khz}.wav"

    def _open_file(self, path: Path) -> None:
        self._close_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        wav = wave.open(str(path), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(self.audio_rate)
        self._wav = wav
        self._current_path = path
        self._has_real_audio = False
        self._pending_silence = []

    def _write_raw(self, audio: np.ndarray) -> None:
        pcm = np.clip(audio, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype(np.int16)
        self._wav.writeframes(pcm.tobytes())

    def _close_file(self) -> None:
        if self._wav is None:
            return
        # Unflushed silence is the trailing tail - dropped.
        self._pending_silence = []
        wav, self._wav = self._wav, None
        self._current_path = None
        try:
            wav.close()  # patches the header: can fail on a full disk
        except OSError:
            pass
