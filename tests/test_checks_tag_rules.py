"""ID3 tags inside FLACs (stripped, unless scene), uncompressed FLACs, the dual-ID3-tag MP3 case,
and the shared in-torrent path helper."""

import shutil
import struct
import subprocess

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import TIT2, ID3UnsupportedVersionError
from mutagen.mp3 import MP3

from salmon.checks import tag_rules
from salmon.checks.tag_rules import (
    has_blank_id3v2_alongside_id3v1,
    has_id3_tag,
    in_torrent_path,
    is_uncompressed,
    process_tag_issues,
)

FRAME_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])
FRAME_SIZE = 417


def _flac_bytes(body: bytes = b"") -> bytes:
    return b"fLaC" + body


def _write_flac(path, *, title: str | None = None) -> None:
    """Write a minimal but mutagen-readable FLAC: a STREAMINFO block, optionally tagged."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    if title is not None:
        tagged = FLAC(str(path))
        tagged["title"] = title
        tagged.save()


def _prepend_id3v2_header(path) -> None:
    header = b"ID3" + bytes([3, 0, 0]) + (0).to_bytes(4, "big")
    path.write_bytes(header + path.read_bytes())


def _append_id3v1_block(path) -> None:
    path.write_bytes(path.read_bytes() + b"TAG" + b"\x00" * 125)


def _write_mp3_frames(path) -> None:
    """A minimal, silent, single-frame-repeated MP3 with no ID3 tag at all."""
    frame = FRAME_HEADER + bytes(FRAME_SIZE - len(FRAME_HEADER))
    path.write_bytes(frame * 3)


def _id3v1_tag(title: bytes = b"Hello") -> bytes:
    """A minimal, valid 128-byte ID3v1 tag."""
    return b"TAG" + title.ljust(30, b"\x00") + b"\x00" * 30 + b"\x00" * 30 + b"\x00" * 4 + b"\x00" * 30 + b"\x00"


def _write_mp3_v1_only(path) -> None:
    _write_mp3_frames(path)
    path.write_bytes(path.read_bytes() + _id3v1_tag())


def _write_mp3_v2_only(path) -> None:
    _write_mp3_frames(path)
    mut = MP3(str(path))
    mut.add_tags()
    assert mut.tags is not None
    mut.tags.add(TIT2(encoding=3, text="Hello"))
    mut.save(v1=0)


def _write_mp3_v1_and_blank_v2(path) -> None:
    _write_mp3_frames(path)
    mut = MP3(str(path))
    mut.add_tags()
    mut.save(v1=2)


def _write_mp3_v1_and_good_v2(path) -> None:
    _write_mp3_frames(path)
    mut = MP3(str(path))
    mut.add_tags()
    assert mut.tags is not None
    mut.tags.add(TIT2(encoding=3, text="Hello"))
    mut.save(v1=2)


def test_has_id3_tag_sees_a_leading_id3v2_header(tmp_path) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 300)

    assert has_id3_tag(str(path)) is True


def test_has_id3_tag_sees_a_trailing_id3v1_block(tmp_path) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(_flac_bytes(b"\x00" * 300) + b"TAG" + b"\x00" * 125)

    assert has_id3_tag(str(path)) is True


def test_has_id3_tag_is_false_for_a_clean_flac(tmp_path) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(_flac_bytes(b"\x00" * 300))

    assert has_id3_tag(str(path)) is False


def test_has_id3_tag_is_false_for_a_file_too_small_to_hold_id3v1(tmp_path) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(_flac_bytes())

    assert has_id3_tag(str(path)) is False


def test_uncompressed_flac_is_flagged_at_the_raw_pcm_rate() -> None:
    raw = 44100 * 16 * 2
    track = {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": raw}

    assert is_uncompressed(track) is True


def test_a_well_compressed_flac_is_not_flagged() -> None:
    raw = 44100 * 16 * 2
    track = {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": int(raw * 0.6)}

    assert is_uncompressed(track) is False


def test_an_incomplete_record_is_never_called_uncompressed() -> None:
    raw = 44100 * 16 * 2

    assert is_uncompressed({"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": None}) is False
    assert is_uncompressed({"sample rate": 44100, "precision": 16, "channels": None, "bit rate": raw}) is False
    assert is_uncompressed({"sample rate": None, "precision": 16, "channels": 2, "bit rate": raw}) is False


def test_in_torrent_path_joins_folder_and_relative_path() -> None:
    assert in_torrent_path("Album", "CD1/01 - Track.flac") == "Album/CD1/01 - Track.flac"
    assert in_torrent_path("Album", ".") == "Album"
    assert in_torrent_path("Album", "") == "Album"


def test_dual_id3_is_flagged_only_for_a_filled_v1_next_to_a_blank_v2(tmp_path) -> None:
    v1_only = tmp_path / "v1_only.mp3"
    v2_only = tmp_path / "v2_only.mp3"
    v1_and_blank_v2 = tmp_path / "v1_and_blank_v2.mp3"
    v1_and_good_v2 = tmp_path / "v1_and_good_v2.mp3"
    _write_mp3_v1_only(v1_only)
    _write_mp3_v2_only(v2_only)
    _write_mp3_v1_and_blank_v2(v1_and_blank_v2)
    _write_mp3_v1_and_good_v2(v1_and_good_v2)

    assert has_blank_id3v2_alongside_id3v1(str(v1_only)) is False
    assert has_blank_id3v2_alongside_id3v1(str(v2_only)) is False
    assert has_blank_id3v2_alongside_id3v1(str(v1_and_blank_v2)) is True
    assert has_blank_id3v2_alongside_id3v1(str(v1_and_good_v2)) is False


def test_a_tag_mutagen_cannot_parse_is_not_flagged_and_does_not_crash(tmp_path, monkeypatch) -> None:
    """A warning check must never raise: a malformed ID3v2 tag mutagen refuses to parse (not just a
    missing one) must be treated as not the flagged case, not propagated to abort the upload."""
    path = tmp_path / "a.mp3"
    path.write_bytes(b"ID3\x02\x00\x00\x00\x00\x00\x00" + b"\x00" * 300 + b"TAG" + b"\x00" * 125)

    def raise_unsupported(_filepath):
        raise ID3UnsupportedVersionError("mutagen cannot parse this ID3v2 version")

    monkeypatch.setattr(tag_rules, "ID3", raise_unsupported)

    assert has_blank_id3v2_alongside_id3v1(str(path)) is False


def test_stripping_removes_a_leading_id3v2_header_but_keeps_the_stream_and_tags(tmp_path) -> None:
    path = tmp_path / "01.flac"
    _write_flac(path, title="Hello")
    _prepend_id3v2_header(path)
    assert has_id3_tag(str(path)) is True

    messages = process_tag_issues(str(tmp_path), {}, scene=False, recompress=False)

    assert has_id3_tag(str(path)) is False
    assert FLAC(str(path))["title"] == ["Hello"]
    assert FLAC(str(path)).info.sample_rate == 44100
    assert messages == ["Removed an ID3 tag from 01.flac (RED and OPS do not allow ID3 tags in FLAC files)."]


def test_stripping_removes_a_trailing_id3v1_block_but_keeps_the_stream_and_tags(tmp_path) -> None:
    path = tmp_path / "01.flac"
    _write_flac(path, title="Hello")
    _append_id3v1_block(path)
    assert has_id3_tag(str(path)) is True

    messages = process_tag_issues(str(tmp_path), {}, scene=False, recompress=False)

    assert has_id3_tag(str(path)) is False
    assert FLAC(str(path))["title"] == ["Hello"]
    assert messages == ["Removed an ID3 tag from 01.flac (RED and OPS do not allow ID3 tags in FLAC files)."]


@pytest.mark.skipif(shutil.which("flac") is None, reason="flac is not installed")
def test_stripping_leaves_a_file_flac_still_considers_valid(tmp_path) -> None:
    path = tmp_path / "01.flac"
    _write_flac(path, title="Hello")
    _prepend_id3v2_header(path)

    process_tag_issues(str(tmp_path), {}, scene=False, recompress=False)

    result = subprocess.run(["flac", "-t", str(path)], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()


def test_a_scene_release_is_never_touched_and_only_warned_about(tmp_path) -> None:
    path = tmp_path / "01.flac"
    _write_flac(path, title="Hello")
    _prepend_id3v2_header(path)
    before = path.read_bytes()

    messages = process_tag_issues(str(tmp_path), {}, scene=True, recompress=False)

    assert path.read_bytes() == before, "a scene release's files must never be modified"
    assert has_id3_tag(str(path)) is True
    assert messages == ["01.flac: FLAC file contains an ID3 tag (RED and OPS do not allow ID3 tags in FLAC files)."]


def test_uncompressed_warning_is_skipped_when_recompress_is_set(tmp_path) -> None:
    raw = 44100 * 16 * 2
    path = tmp_path / "01.flac"
    _write_flac(path)
    audio_info = {"01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": raw}}

    with_recompress = process_tag_issues(str(tmp_path), audio_info, scene=False, recompress=True)
    without_recompress = process_tag_issues(str(tmp_path), audio_info, scene=False, recompress=False)

    assert with_recompress == []
    assert without_recompress == [
        "01.flac: FLAC file looks uncompressed (RED and OPS can trump it); recompress it with salmon up -c."
    ]


def test_process_tag_issues_handles_id3_flacs_uncompressed_flacs_and_dual_id3_mp3s(tmp_path) -> None:
    raw = 44100 * 16 * 2
    flac_with_id3 = tmp_path / "01.flac"
    _write_flac(flac_with_id3)
    _prepend_id3v2_header(flac_with_id3)
    _write_flac(tmp_path / "02.flac")
    _write_mp3_v1_and_blank_v2(tmp_path / "03.mp3")
    _write_mp3_v2_only(tmp_path / "04.mp3")

    audio_info = {
        "01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": int(raw * 0.6)},
        "02.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": raw},
        "03.mp3": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": int(raw * 0.2)},
        "04.mp3": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": int(raw * 0.2)},
    }

    messages = process_tag_issues(str(tmp_path), audio_info, scene=False, recompress=False)

    assert messages == [
        "Removed an ID3 tag from 01.flac (RED and OPS do not allow ID3 tags in FLAC files).",
        "02.flac: FLAC file looks uncompressed (RED and OPS can trump it); recompress it with salmon up -c.",
        "03.mp3: MP3 file has a filled-in ID3v1 tag and a blank ID3v2 tag (RED and OPS can trump it).",
    ]


def test_a_clean_compressed_flac_has_no_messages(tmp_path) -> None:
    path = tmp_path / "01.flac"
    _write_flac(path)
    audio_info = {"01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": 900_000}}

    assert process_tag_issues(str(tmp_path), audio_info, scene=False, recompress=False) == []
