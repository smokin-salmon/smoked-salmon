"""Helpers of tests/test_uploader_shortened_conversions.py that other test modules use too."""

import struct
from pathlib import Path

from mutagen.flac import FLAC


def _write_flac(path: Path, rate: int, bits: int, **tags: str) -> None:
    """Write a FLAC file with no audio: a STREAMINFO block at this rate and depth, and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((rate << 44) | (1 << 41) | ((bits - 1) << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()
