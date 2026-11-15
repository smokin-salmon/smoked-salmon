"""Small audio files and albums for tests: FLACs with no audio, written byte for byte."""

import struct
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC


def write_flac(path: Path, *, bits: int = 16, rate: int = 44100, **tags: str) -> Path:
    """Write a FLAC with no audio: a STREAMINFO block saying `bits` and `rate`, and `tags` as Vorbis comments.

    Creates the parent folder. Returns `path`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((rate << 44) | (1 << 41) | ((bits - 1) << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()
    return path


def album(folder: Path, *, tracks: int = 2, tags: dict[str, str] | None = None, cover: bool = False) -> Path:
    """Write `tracks` FLACs (`01.flac`, `02.flac`...) in `folder`, each tagged TRACKNUMBER plus `tags`.

    `cover=True` also writes a `cover.jpg`. Returns `folder`.
    """
    for number in range(1, tracks + 1):
        track_tags: dict[str, Any] = {**(tags or {}), "TRACKNUMBER": str(number)}
        write_flac(folder / f"{number:02d}.flac", **track_tags)
    if cover:
        (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder
