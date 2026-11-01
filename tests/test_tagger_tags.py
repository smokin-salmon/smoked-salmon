import struct
from unittest.mock import MagicMock

import anyio
import asyncclick as click
from mutagen import mp4
from mutagen.flac import FLAC, VCFLACDict
from mutagen.id3 import TIT2, TPE1, TPOS, TRCK
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4Tags

from salmon import cfg
from salmon.tagger.tagfile import TagFile
from salmon.tagger.tags import check_tags, print_a_tag, standardize_tags


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


def _fake_mp4_tagfile(*, title=None, artist=None) -> TagFile:
    """Build a TagFile wrapping a mocked mutagen.mp4.MP4 without needing a real m4a container."""
    tags = MP4Tags()
    if title is not None:
        tags["\xa9nam"] = [title]
    if artist is not None:
        tags["\xa9ART"] = [artist]

    mut = MagicMock(spec=mp4.MP4)
    mut.tags = tags

    tag_file = TagFile.__new__(TagFile)
    object.__setattr__(tag_file, "mut", mut)
    return tag_file


async def _check_tags_with_prompt_puddletag_does_not_crash(tmp_path, monkeypatch, capsys):
    """check_tags used to raise TypeError: 'NoneType' object is not callable here (#355)."""
    _write_flac(tmp_path / "01.flac", title="A Song", artist="An Artist")

    monkeypatch.setattr(cfg.upload, "prompt_puddletag", True)
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)

    tags = await check_tags(str(tmp_path))

    assert list(tags.keys()) == ["01.flac"]

    captured = capsys.readouterr()
    assert "> title: A Song" in captured.out
    assert "> artist: An Artist" in captured.out


def test_check_tags_with_prompt_puddletag_does_not_crash(tmp_path, monkeypatch, capsys) -> None:
    anyio.run(lambda: _check_tags_with_prompt_puddletag_does_not_crash(tmp_path, monkeypatch, capsys))


def test_print_a_tag_flac(tmp_path, capsys):
    _write_flac(tmp_path / "01.flac", title="A Song", artist="An Artist")
    print_a_tag(TagFile(str(tmp_path / "01.flac")))

    out = capsys.readouterr().out
    assert "> title: A Song" in out
    assert "> artist: An Artist" in out


def test_flac_tag_repeated_with_the_same_value_reads_once(tmp_path):
    """Some store FLACs carry TRACKNUMBER=1 twice: read as "1; 1", it never matched the metadata's "1"."""
    _write_flac(tmp_path / "01.flac")
    tagged = FLAC(tmp_path / "01.flac")
    tagged["tracknumber"] = ["1", "1"]
    tagged["isrc"] = ["GBABC2600001", "GBABC2600001"]
    tagged.save()

    tag_file = TagFile(str(tmp_path / "01.flac"))

    assert tag_file.tracknumber == "1"
    assert tag_file.isrc == "GBABC2600001"


def test_flac_artists_and_genres_repeated_with_the_same_value_read_once(tmp_path):
    _write_flac(tmp_path / "01.flac")
    tagged = FLAC(tmp_path / "01.flac")
    tagged["artist"] = ["Artist", "Guest", "Artist"]
    tagged["artists"] = ["Artist", "Artist"]
    tagged["genre"] = ["Rock", "Rock"]
    tagged.save()

    tag_file = TagFile(str(tmp_path / "01.flac"))

    assert tag_file.artist == ["Artist", "Guest"]
    assert tag_file.artists == ["Artist"]
    assert tag_file.genre == ["Rock"]


def test_flac_tag_with_repeated_and_different_values_keeps_each_once_in_order(tmp_path):
    _write_flac(tmp_path / "01.flac")
    tagged = FLAC(tmp_path / "01.flac")
    tagged["comment"] = ["second", "first", "second"]
    tagged.save()

    assert TagFile(str(tmp_path / "01.flac")).comment == "second; first"


def test_print_a_tag_mp3(tmp_path, capsys):
    _write_mp3(tmp_path / "01.mp3", title="A Song", artist="An Artist")
    print_a_tag(TagFile(str(tmp_path / "01.mp3")))

    out = capsys.readouterr().out
    assert "> title: A Song" in out
    assert "> artist: An Artist" in out


def test_print_a_tag_mp4(capsys):
    tag_file = _fake_mp4_tagfile(title="A Song", artist="An Artist")
    print_a_tag(tag_file)

    out = capsys.readouterr().out
    assert "> title: A Song" in out
    assert "> artist: An Artist" in out


def test_print_a_tag_no_tags_prints_nothing(tmp_path, capsys):
    _write_flac(tmp_path / "01.flac")
    print_a_tag(TagFile(str(tmp_path / "01.flac")))

    out = capsys.readouterr().out
    assert out == ""


def test_standardize_tags_writes_uppercase_vorbis_comment_keys(tmp_path):
    """Renaming an alias writes the canonical comment, and mutagen keeps the case it is given.

    The alias map is lowercase, so a file tagged YEAR came back as date. Vorbis comment names are
    case-insensitive, but the spec recommends uppercase.
    """
    _write_flac(
        tmp_path / "01.flac",
        YEAR="1999",
        TOTALTRACKS="12",
        ARTIST="Artist",
        ENCODER="reference",
    )
    _write_flac(tmp_path / "02.flac", year="2001", TITLE="Song")

    standardize_tags(str(tmp_path))

    first = FLAC(tmp_path / "01.flac")
    comments = first.tags
    assert isinstance(comments, VCFLACDict)
    # Iterate the comment, not .keys(): that normalizes every name to lowercase.
    assert {key for key, _value in comments} == {"DATE", "TRACKTOTAL", "ARTIST", "ENCODER"}
    assert first["date"] == ["1999"]
    assert first["tracktotal"] == ["12"]
    assert first["artist"] == ["Artist"]
    assert "year" not in comments
    assert "totaltracks" not in comments

    second = FLAC(tmp_path / "02.flac")
    second_comments = second.tags
    assert isinstance(second_comments, VCFLACDict)
    assert {key for key, _value in second_comments} == {"DATE", "TITLE"}
    assert second["date"] == ["2001"]
    assert second["title"] == ["Song"]
    assert "year" not in second_comments


def test_an_mp3_number_without_a_total_gives_no_total(tmp_path):
    _write_mp3(tmp_path / "01.mp3", title="A Song")
    mut = MP3(tmp_path / "01.mp3")
    assert mut.tags is not None
    mut.tags.add(TRCK(encoding=3, text="3"))
    mut.tags.add(TPOS(encoding=3, text="1"))
    mut.save()

    tag = TagFile(str(tmp_path / "01.mp3"))

    assert (tag.tracknumber, tag.tracktotal, tag.discnumber, tag.disctotal) == ("3", None, "1", None)
