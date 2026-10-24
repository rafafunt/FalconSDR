"""Embeds the scanner's settings at recording time (gain, trigger, window,
...) as JSON in a standard RIFF ``LIST``/``INFO``/``ICMT`` chunk - players
skip it, ffprobe/exiftool show it.

Call only after ``wave.Wave_write.close()``, or the chunk sizes are stale.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any


def append_metadata_chunk(path: Path, metadata: dict[str, Any]) -> None:
    """Appends the metadata chunk and fixes up the master RIFF chunk's
    overall size field (bytes 4:8, "file size - 8") to include it."""
    payload = json.dumps(metadata, ensure_ascii=False, default=str).encode("utf-8")
    if len(payload) % 2:
        payload += b"\x00"  # RIFF (sub)chunks must be word-aligned
    icmt = b"ICMT" + struct.pack("<I", len(payload)) + payload
    list_data = b"INFO" + icmt
    chunk = b"LIST" + struct.pack("<I", len(list_data)) + list_data

    with path.open("r+b") as f:
        f.seek(0, 2)
        f.write(chunk)
        file_size = f.tell()
        f.seek(4)
        f.write(struct.pack("<I", file_size - 8))


def read_metadata_chunk(path: Path) -> dict[str, Any] | None:
    """Reads back what append_metadata_chunk() wrote, if present. Not used
    anywhere in the app yet (metadata is meant for external tools/future
    use) but kept alongside the writer for symmetry."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    idx = data.find(b"INFOICMT")
    if idx == -1:
        return None
    size_off = idx + len(b"INFOICMT")
    if size_off + 4 > len(data):
        return None
    (size,) = struct.unpack_from("<I", data, size_off)
    payload = data[size_off + 4: size_off + 4 + size]
    try:
        return json.loads(payload.rstrip(b"\x00").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
