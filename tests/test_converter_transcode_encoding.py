"""What the MP3 encoding step writes, and that a cancel stops it."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import pytest
from helpers.tagger_tags import _write_mp3
from helpers.uploader_shortened_conversions import _write_flac
from mutagen.id3 import ID3, PictureType

from salmon import cfg, interaction
from salmon.converter import conversions, transcoding
from salmon.errors import UploadError
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


# A track reaches its name only complete and tagged


def _album(tmp_path: Path, names: tuple[str, ...] = ("01. one", "02. two")) -> Path:
    album = tmp_path / "source" / "Album [WEB FLAC]"
    album.mkdir(parents=True)
    for name in names:
        _write_flac(album / f"{name}.flac", 44100, 16, title=name)
    return album


def _output(album: Path, tmp_path: Path) -> Path:
    return Path(transcoding._build_output_path(str(album), "V0", str(tmp_path)))


def _transcode(album: Path, tmp_path: Path) -> Path:
    return Path(anyio.run(lambda: transcoding.transcode_folder(str(album), "V0", output_dir=str(tmp_path))))


def _files_in(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.rglob("*") if path.is_file())


def _fake_lame(monkeypatch, failing: frozenset[str] = frozenset()) -> list[str]:
    """Encode one track at a time, in order, writing a small MP3 at once as lame does. Returns the tracks given."""
    encoded: list[str] = []

    async def encode(_quality: str, flac_path: str, mp3_path: str) -> None:
        name = os.path.basename(flac_path)
        encoded.append(name)
        _write_mp3(Path(mp3_path))
        if name in failing:
            raise RuntimeError("lame: No space left on device")

    monkeypatch.setattr(transcoding, "_flac_to_mp3", encode)
    monkeypatch.setattr(cfg.upload, "simultaneous_threads", 1)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return encoded


def test_a_track_that_failed_to_encode_leaves_nothing(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path)
    _fake_lame(monkeypatch, failing=frozenset({"02. two.flac"}))

    with pytest.raises(ExceptionGroup):
        _transcode(album, tmp_path)

    assert _files_in(_output(album, tmp_path)) == ["01. one.mp3"]


def test_a_track_whose_tags_failed_leaves_nothing(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path)
    _fake_lame(monkeypatch)

    def fail(*_args: Any) -> None:
        raise ValueError("no tags")

    monkeypatch.setattr(transcoding, "_copy_tags", fail)

    with pytest.raises(ExceptionGroup):
        _transcode(album, tmp_path)

    assert _files_in(_output(album, tmp_path)) == []


def test_a_cancelled_track_leaves_nothing(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path)
    _fake_lame(monkeypatch)

    async def run() -> None:
        started = anyio.Event()

        async def stuck(_quality: str, _flac_path: str, mp3_path: str) -> None:
            _write_mp3(Path(mp3_path))
            started.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(transcoding, "_flac_to_mp3", stuck)
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: transcoding.transcode_folder(str(album), "V0", output_dir=str(tmp_path)))
            await started.wait()
            group.cancel_scope.cancel()

    anyio.run(run)

    assert _files_in(_output(album, tmp_path)) == []


def test_a_transcode_stopped_partway_is_made_again_not_reused(monkeypatch, tmp_path, capsys) -> None:
    album = _album(tmp_path)
    _fake_lame(monkeypatch, failing=frozenset({"02. two.flac"}))
    with pytest.raises(ExceptionGroup):
        _transcode(album, tmp_path)
    encoded = _fake_lame(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(click, "confirm", lambda text, **_kwargs: asked.append(text) or True)

    out = _transcode(album, tmp_path)

    assert len(asked) == 1
    assert encoded == ["01. one.flac", "02. two.flac"]
    assert _files_in(out) == ["01. one.mp3", "02. two.mp3"]
    assert conversions.conversion_of(str(out)) is not None
    assert "already exists" not in capsys.readouterr().out


def test_a_transcode_stopped_partway_is_refused_with_yyy_not_reused(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path)
    _fake_lame(monkeypatch, failing=frozenset({"02. two.flac"}))
    with pytest.raises(ExceptionGroup):
        _transcode(album, tmp_path)
    encoded = _fake_lame(monkeypatch)

    async def run() -> str:
        with interaction.assuming_defaults(True):
            return await transcoding.transcode_folder(str(album), "V0", output_dir=str(tmp_path))

    with pytest.raises(UploadError, match="with -yyy"):
        anyio.run(run)

    assert encoded == []


# As before


def test_a_finished_track_is_at_its_name_tagged_and_alone(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path)
    _fake_lame(monkeypatch)

    out = _transcode(album, tmp_path)

    assert _files_in(out) == ["01. one.mp3", "02. two.mp3"]
    assert ID3(out / "01. one.mp3")["TIT2"].text == ["01. one"]
    assert ID3(out / "02. two.mp3")["TIT2"].text == ["02. two"]


def test_a_track_with_the_longest_name_a_folder_holds_is_transcoded(monkeypatch, tmp_path) -> None:
    # 253 bytes as an MP3: anything added to the name would be over the filesystem's 255.
    name = "x" * 249
    album = _album(tmp_path, (name,))
    _fake_lame(monkeypatch)

    out = _transcode(album, tmp_path)

    assert _files_in(out) == [f"{name}.mp3"]


def test_what_a_killed_run_left_beside_its_tracks_does_not_count_and_goes_with_the_folder(
    monkeypatch, tmp_path
) -> None:
    album = _album(tmp_path)
    out = _output(album, tmp_path)
    _write_mp3(out / "01. one.mp3")
    (out / ".0123456789abcdef.part").write_bytes(b"cut short")
    encoded = _fake_lame(monkeypatch)
    monkeypatch.setattr(click, "confirm", lambda _text, **_kwargs: True)

    _transcode(album, tmp_path)

    assert encoded == ["01. one.flac", "02. two.flac"]
    assert _files_in(out) == ["01. one.mp3", "02. two.mp3"]


def test_a_complete_folder_with_no_record_is_reused(monkeypatch, tmp_path, capsys) -> None:
    album = _album(tmp_path)
    _fake_lame(monkeypatch)
    out = _transcode(album, tmp_path)
    os.remove(conversions._sidecar(str(out)))
    encoded = _fake_lame(monkeypatch)

    again = _transcode(album, tmp_path)

    assert again == out
    assert encoded == []
    assert f"{out} already exists." in capsys.readouterr().out
