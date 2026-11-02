"""Helpers of tests/test_tagger_tags.py that other test modules use too."""

import struct

from mutagen.flac import FLAC
from mutagen.id3 import TIT2, TPE1
from mutagen.mp3 import MP3


def _write_flac(path, *, title=None, artist=None, **tags: str) -> None:
    """Write a FLAC file with no audio: just a STREAMINFO block, optionally tagged.

    Extra tags keep the case of the keyword: mutagen stores the comment name it is given.
    """
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    if title is not None or artist is not None or tags:
        tagged = FLAC(path)
        if title is not None:
            tagged["title"] = title
        if artist is not None:
            tagged["artist"] = artist
        for key, value in tags.items():
            tagged[key] = value
        tagged.save()


def _write_mp3(path, *, title=None, artist=None) -> None:
    """Write a minimal single-frame MP3 (silent, no real audio) with optional ID3 tags."""
    frame_header = bytes([0xFF, 0xFB, 0x90, 0x00])
    frame_size = 417
    frame = frame_header + bytes(frame_size - len(frame_header))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame * 3)
    if title is not None or artist is not None:
        mut = MP3(path)
        mut.add_tags()
        assert mut.tags is not None
        if title is not None:
            mut.tags.add(TIT2(encoding=3, text=title))
        if artist is not None:
            mut.tags.add(TPE1(encoding=3, text=artist))
        mut.save()
