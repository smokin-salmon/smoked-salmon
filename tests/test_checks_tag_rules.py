"""ID3 tags inside FLACs, uncompressed FLACs, the dual-ID3-tag MP3 case, and the shared
in-torrent path helper."""

from mutagen.id3 import TIT2, ID3UnsupportedVersionError
from mutagen.mp3 import MP3

from salmon.checks import tag_rules
from salmon.checks.tag_rules import (
    collect_tag_warnings,
    has_blank_id3v2_alongside_id3v1,
    has_id3_tag,
    in_torrent_path,
    is_uncompressed,
)

FRAME_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])
FRAME_SIZE = 417


def _flac_bytes(body: bytes = b"") -> bytes:
    return b"fLaC" + body


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


def test_collect_tag_warnings_flags_id3_and_uncompressed_flacs_and_dual_id3_mp3s(tmp_path) -> None:
    raw = 44100 * 16 * 2
    (tmp_path / "01.flac").write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 300)
    (tmp_path / "02.flac").write_bytes(_flac_bytes(b"\x00" * 300))
    _write_mp3_v1_and_blank_v2(tmp_path / "03.mp3")
    _write_mp3_v2_only(tmp_path / "04.mp3")

    audio_info = {
        "01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": int(raw * 0.6)},
        "02.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": raw},
        "03.mp3": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": int(raw * 0.2)},
        "04.mp3": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": int(raw * 0.2)},
    }

    warnings = collect_tag_warnings(str(tmp_path), audio_info)

    assert warnings == [
        "01.flac: FLAC file contains an ID3 tag "
        "(RED and OPS do not allow ID3 tags in FLAC files); sanitizing removes it.",
        "02.flac: FLAC file looks uncompressed (RED and OPS can trump it); recompress it with salmon up -c.",
        "03.mp3: MP3 file has a filled-in ID3v1 tag and a blank ID3v2 tag (RED and OPS can trump it).",
    ]


def test_a_clean_compressed_flac_has_no_warnings(tmp_path) -> None:
    (tmp_path / "01.flac").write_bytes(_flac_bytes(b"\x00" * 300))
    audio_info = {"01.flac": {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": 900_000}}

    assert collect_tag_warnings(str(tmp_path), audio_info) == []
