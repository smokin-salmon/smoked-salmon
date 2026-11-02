"""What the MP3 encoding step writes, and that a cancel stops it."""

import asyncio
from types import SimpleNamespace

import anyio
import pytest
from helpers.tagger_tags import _write_mp3
from mutagen.id3 import ID3, PictureType

from salmon.converter import transcoding
from salmon.tagger.cover import MAX_PICTURES_AND_PADDING

MIB = 1024 * 1024


def _picture(kind: PictureType, size: int, desc: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(mime="image/jpeg", type=kind, desc=str(kind) if desc is None else desc, data=b"x" * size)


def test_transcode_embeds_only_the_pictures_that_fit(tmp_path) -> None:
    mp3_path = tmp_path / "01.mp3"
    _write_mp3(mp3_path)
    pictures = [
        _picture(PictureType.COVER_FRONT, 2 * MIB),
        _picture(PictureType.COVER_BACK, 500 * 1024),
        _picture(PictureType.MEDIA, 600 * 1024),
    ]

    transcoding._copy_tags({"title": ["Song"]}, SimpleNamespace(pictures=pictures), mp3_path)  # type: ignore[arg-type]

    saved = ID3(mp3_path)
    assert [len(frame.data) for frame in saved.getall("APIC")] == [500 * 1024]
    assert saved.size <= MAX_PICTURES_AND_PADDING
    assert MAX_PICTURES_AND_PADDING == MIB


def test_a_picture_that_fits_only_without_its_frame_and_padding_is_left_out(tmp_path) -> None:
    # Its data alone is under the limit; with the frame, the text and the padding mutagen saves, the tag is not.
    # mutagen pads by the audio's size (0.1% of it): a 20 MB track gets about 21 KiB.
    mp3_path = tmp_path / "01.mp3"
    _write_mp3(mp3_path)
    mp3_path.write_bytes(mp3_path.read_bytes() * (20 * MIB // 1251))
    pictures = [_picture(PictureType.COVER_FRONT, MAX_PICTURES_AND_PADDING - 9 * 1024)]

    transcoding._copy_tags({"title": ["Song"]}, SimpleNamespace(pictures=pictures), mp3_path)  # type: ignore[arg-type]

    saved = ID3(mp3_path)
    assert saved.getall("APIC") == []
    assert saved.size <= MAX_PICTURES_AND_PADDING


def test_a_picture_too_big_for_the_tag_leaves_the_one_of_its_desc_before_it(tmp_path) -> None:
    # One picture per desc in an ID3 tag: the later one replaces the earlier only when it fits.
    mp3_path = tmp_path / "01.mp3"
    _write_mp3(mp3_path)
    pictures = [_picture(PictureType.COVER_FRONT, 500 * 1024, ""), _picture(PictureType.COVER_BACK, 2 * MIB, "")]

    transcoding._copy_tags({"title": ["Song"]}, SimpleNamespace(pictures=pictures), mp3_path)  # type: ignore[arg-type]

    assert [len(frame.data) for frame in ID3(mp3_path).getall("APIC")] == [500 * 1024]


def test_a_picture_left_out_leaves_no_padding_over_the_limit(tmp_path) -> None:
    # mutagen keeps freed space as padding up to about 1% of the audio: about 210 KiB on a 20 MB track.
    mp3_path = tmp_path / "01.mp3"
    _write_mp3(mp3_path)
    mp3_path.write_bytes(mp3_path.read_bytes() * (20 * MIB // 1251))
    pictures = [_picture(PictureType.COVER_FRONT, 900 * 1024), _picture(PictureType.COVER_BACK, 150 * 1024)]

    transcoding._copy_tags({"title": ["Song"]}, SimpleNamespace(pictures=pictures), mp3_path)  # type: ignore[arg-type]

    saved = ID3(mp3_path)
    assert [len(frame.data) for frame in saved.getall("APIC")] == [900 * 1024]
    assert saved.size <= MAX_PICTURES_AND_PADDING


def test_a_cancelled_transcode_kills_flac_and_lame(monkeypatch, tmp_path) -> None:
    spawned: list = []

    class Encoder:
        returncode = None

        def __init__(self) -> None:
            self.killed = False
            self.waited = False

        async def communicate(self):
            raise asyncio.CancelledError

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            return -9

    async def spawn(*_args, **_kwargs) -> Encoder:
        spawned.append(Encoder())
        return spawned[-1]

    monkeypatch.setattr(transcoding.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(asyncio.CancelledError):
        anyio.run(transcoding._flac_to_mp3, "V0", str(tmp_path / "01.flac"), str(tmp_path / "01.mp3"))

    assert [encoder.killed for encoder in spawned] == [True, True]
    assert [encoder.waited for encoder in spawned] == [True, True]
