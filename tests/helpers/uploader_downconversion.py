"""Helpers of tests/test_uploader_downconversion.py that other test modules use too."""

import struct
from pathlib import Path

SOURCE_NAMES = ("01. Track.flac", "02. Track.flac")


def _write_flac(path: Path, sample_rate: int, bits: int) -> None:
    """Write a FLAC with only a STREAMINFO block, enough for its audio info to be read."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((sample_rate << 44) | (1 << 41) | ((bits - 1) << 36) | sample_rate * 200).to_bytes(8, "big")
    streaminfo += bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
