"""The integrity check of a DSD file (#919), from its headers: neither DSF nor DSDIFF holds a checksum of its audio.

What can be found is a file shorter than its headers say, the damage of a download that stopped: its last samples,
or its tags, are missing.
"""

import os
import struct
from typing import IO

from mutagen import MutagenError
from mutagen.dsdiff import DSDIFF
from mutagen.dsf import DSF

# DSF: the "DSD " chunk (its size, the file's, and where the ID3 tag starts), then "fmt ", then "data".
_DSF_HEADER = struct.Struct("<4sQQQ")
_DSF_CHUNK = struct.Struct("<4sQ")
# DSDIFF: an "FRM8" chunk of form "DSD " holds every other chunk; each is an ID, a size, and its data, padded to even.
_DFF_FORM = struct.Struct(">4sQ4s")
_DFF_CHUNK = struct.Struct(">4sQ")


def dsd_damage(path: str) -> str | None:
    """What is wrong with the DSF or DSDIFF file at path, as its headers tell; None when nothing is.

    Raises:
        OSError: If the file cannot be read.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        head = handle.read(_DSF_HEADER.size)
        if head[:4] == b"DSD ":
            problem = _dsf_damage(handle, head, size)
        elif head[:4] == b"FRM8":
            problem = _dff_damage(handle, head, size)
        else:
            return "it is neither a DSF nor a DSDIFF file"
    if problem:
        return problem
    try:
        (DSF if head[:4] == b"DSD " else DSDIFF)(path)
    except MutagenError as error:
        return f"its headers cannot be read ({error})"
    return None


def _short(size: int, needed: int, what: str) -> str:
    return f"it is cut short: {size} bytes, where its {what} needs {needed}"


def _dsf_damage(handle: IO[bytes], head: bytes, size: int) -> str | None:
    if len(head) < _DSF_HEADER.size:
        return _short(size, _DSF_HEADER.size, "header")
    _, header_size, total_size, metadata_at = _DSF_HEADER.unpack(head)
    if total_size > size:
        return _short(size, total_size, "header")
    # "fmt " follows the DSD chunk, and "data" follows "fmt ".
    offset = header_size
    for expected in (b"fmt ", b"data"):
        handle.seek(offset)
        chunk = handle.read(_DSF_CHUNK.size)
        if len(chunk) < _DSF_CHUNK.size:
            return _short(size, offset + _DSF_CHUNK.size, f"{expected.decode().strip()} chunk")
        chunk_id, chunk_size = _DSF_CHUNK.unpack(chunk)
        if chunk_id != expected:
            return f"it has no {expected.decode().strip()} chunk where its header says"
        if offset + chunk_size > size:
            return _short(size, offset + chunk_size, f"{expected.decode().strip()} chunk")
        offset += chunk_size
    if metadata_at and metadata_at >= size:
        return _short(size, metadata_at + 1, "tag")
    return None


def _dff_damage(handle: IO[bytes], head: bytes, size: int) -> str | None:
    if len(head) < _DFF_FORM.size:
        return _short(size, _DFF_FORM.size, "header")
    _, form_size, form_type = _DFF_FORM.unpack(head[: _DFF_FORM.size])
    if form_type != b"DSD ":
        return "it is an IFF file, but not of DSD audio"
    end = _DFF_CHUNK.size + form_size
    if end > size:
        return _short(size, end, "header")
    offset = _DFF_FORM.size
    sound = False
    while offset + _DFF_CHUNK.size <= end:
        handle.seek(offset)
        chunk_id, chunk_size = _DFF_CHUNK.unpack(handle.read(_DFF_CHUNK.size))
        chunk_end = offset + _DFF_CHUNK.size + chunk_size
        if chunk_end > end:
            return _short(size, chunk_end, f"{chunk_id.decode(errors='replace').strip()} chunk")
        sound |= chunk_id in (b"DSD ", b"DST ")
        offset = chunk_end + chunk_size % 2
    if not sound:
        return "it holds no audio chunk"
    return None
