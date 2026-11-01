"""Helpers of tests/test_tagger_multi_value_tags.py that other test modules use too."""

import struct

import av
import numpy as np
from mutagen.flac import FLAC, VCFLACDict
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from salmon.tagger.retagger import (
    collect_album_data,
    create_track_changes,
    retag_files,
)
from salmon.tagger.tagfile import TagFile

FRAME_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])


FRAME_SIZE = 417


def _write_flac(path, **tags) -> None:
    """A FLAC with no audio: a STREAMINFO block and the given Vorbis comments, key case preserved.

    A list value is one field per item.
    """
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()


def _write_mp3(path, *frames) -> None:
    """A silent MP3 with an ID3v2.4 tag holding ``frames``."""
    path.write_bytes((FRAME_HEADER + bytes(FRAME_SIZE - len(FRAME_HEADER))) * 3)
    tags = ID3()
    for frame in frames:
        tags.add(frame)
    tags.save(path, v2_version=4)


def _write_m4a(path, **tags) -> None:
    with av.open(str(path), "w", format="ipod") as container:
        stream = container.add_stream("aac", rate=44100, layout="stereo")
        frame = av.AudioFrame.from_ndarray(np.zeros((2, 2048), dtype=np.float32), format="fltp", layout="stereo")
        frame.sample_rate = 44100
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    audio = MP4(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()


def _trackmeta(artists, title="Song", number="1"):
    return {
        "title": title,
        "isrc": None,
        "track#": number,
        "disc#": "1",
        "tracktotal": None,
        "disctotal": None,
        "artists": artists,
    }


def _metadata(*tracks, genres=("Rap", "Hip Hop")):
    return {
        "title": "Album",
        "edition_title": None,
        "genres": set(genres),
        "group_year": "2020",
        "label": None,
        "catno": None,
        "upc": None,
        "comment": None,
        "artists": [("Dree Low", "main")],
        "tracks": {"1": {str(i): track for i, track in enumerate(tracks, 1)}},
    }


def _gather(tmp_path):
    return {path.name: TagFile(str(path)) for path in sorted(tmp_path.iterdir())}


def _retag(tmp_path, metadata):
    """What ``tag_files`` does after the user agrees, returning what it proposed."""
    tags = _gather(tmp_path)
    album_changes = collect_album_data(metadata)
    track_changes = create_track_changes(tags, metadata)
    retag_files(str(tmp_path), album_changes, track_changes)
    return album_changes, track_changes


def _flac_tags(path):
    """Every comment in the file, in order, name case as stored."""
    tags = FLAC(path).tags
    assert isinstance(tags, VCFLACDict)
    return list(tags)


def _mp3_frames(path):
    """Every ID3 frame as (HashKey, text), in a stable order."""
    tags = MP3(path).tags
    assert tags is not None
    return sorted((key, list(frame.text)) for key, frame in tags.items() if hasattr(frame, "text"))


def _flac_album(tmp_path, **tags):
    _write_flac(tmp_path / "01.flac", TRACKNUMBER="1", DISCNUMBER="1", TITLE="Song", **tags)
    return tmp_path / "01.flac"


def _mp3_album(tmp_path, *frames):
    from mutagen.id3 import TRCK

    _write_mp3(tmp_path / "01.mp3", TRCK(text="1"), *frames)
    return tmp_path / "01.mp3"
