import struct
from unittest.mock import MagicMock

import anyio
import asyncclick as click
from mutagen import mp4
from mutagen.flac import FLAC
from mutagen.id3 import TIT2, TPE1
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4Tags

from salmon import cfg
from salmon.tagger.tagfile import TagFile
from salmon.tagger.tags import check_tags, print_a_tag


def _write_flac(path, *, title=None, artist=None) -> None:
    """Write a FLAC file with no audio: just a STREAMINFO block, optionally tagged."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    if title is not None or artist is not None:
        tagged = FLAC(path)
        if title is not None:
            tagged["title"] = title
        if artist is not None:
            tagged["artist"] = artist
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
