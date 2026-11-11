"""Small DSF and DSDIFF files, built by hand: FFmpeg decodes DSD but writes none.

The samples are DSD's idle pattern (0x69), which decodes to silence. Tags are written by mutagen, as salmon writes
them: in a DSF, an ID3 tag at the end of the file its header points to; in a DSDIFF, an "ID3 " chunk.
"""

import math
import struct
from pathlib import Path

from mutagen import id3
from mutagen.dsdiff import DSDIFF
from mutagen.dsf import DSF

DSD64 = 2822400
DSD128 = 5644800
DSD256 = 11289600

# DSD's silence: as many ones as zeros, in a pattern no modulator settles on.
_IDLE = 0x69
# A DSF's samples are in blocks of 4096 bytes, one channel after the other.
_DSF_BLOCK = 4096


def write_dsf(
    path: Path,
    *,
    rate: int = DSD64,
    channels: int = 2,
    seconds: float = 0.2,
    tags: dict[str, str] | None = None,
    msb_first: bool = False,
) -> Path:
    """Write a DSF file of `seconds` of DSD silence, with these ID3 frames (TIT2: "Title") if any; give its path.

    msb_first stores the bits most significant first, which a DSF's header says as 8 bits per sample, not 1.
    """
    samples = round(rate * seconds)
    blocks = max(1, math.ceil(samples / 8 / _DSF_BLOCK))
    data = bytes([_IDLE]) * (blocks * _DSF_BLOCK * channels)
    bits = 8 if msb_first else 1
    fmt = struct.pack(
        "<4sQIIIIIIQII", b"fmt ", 52, 1, 0, _channel_type(channels), channels, rate, bits, samples, 4096, 0
    )
    data_chunk = struct.pack("<4sQ", b"data", 12 + len(data)) + data
    total = 28 + len(fmt) + len(data_chunk)
    path.write_bytes(struct.pack("<4sQQQ", b"DSD ", 28, total, 0) + fmt + data_chunk)
    if tags is not None:
        _tag(DSF(path), tags)
    return path


def write_dff(
    path: Path, *, rate: int = DSD64, channels: int = 2, seconds: float = 0.2, tags: dict[str, str] | None = None
) -> Path:
    """Write a DSDIFF file of `seconds` of DSD silence, with these ID3 frames if any; give its path."""
    frames = math.ceil(rate * seconds / 8)
    ids = [b"SLFT", b"SRGT"] if channels == 2 else [b"C   "] * channels
    sound = b"SND " + _chunk(b"FS  ", struct.pack(">I", rate))
    sound += _chunk(b"CHNL", struct.pack(">H", channels) + b"".join(ids))
    sound += _chunk(b"CMPR", b"DSD " + bytes([14]) + b"not compressed")
    form = b"DSD " + _chunk(b"FVER", struct.pack(">I", 0x01050000)) + _chunk(b"PROP", sound)
    form += _chunk(b"DSD ", bytes([_IDLE]) * (frames * channels))
    path.write_bytes(_chunk(b"FRM8", form))
    if tags is not None:
        _tag(DSDIFF(path), tags)
    return path


def _chunk(chunk_id: bytes, data: bytes) -> bytes:
    """A DSDIFF chunk: its ID, its size, its data, padded to an even length."""
    return struct.pack(">4sQ", chunk_id, len(data)) + data + b"\0" * (len(data) % 2)


def _channel_type(channels: int) -> int:
    """DSF's code for the channel layout: 1 mono, 2 stereo, 7 for 5.1."""
    return {1: 1, 2: 2, 6: 7}.get(channels, 2)


def _tag(file: DSF | DSDIFF, tags: dict[str, str]) -> None:
    file.add_tags()
    assert file.tags is not None
    for frame, text in tags.items():
        file.tags.add(getattr(id3, frame)(encoding=3, text=text))
    file.save()
