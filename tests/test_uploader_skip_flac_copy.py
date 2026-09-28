"""--skip-flac-upload never modifies the folder it is given: the run works on a scratch copy."""

import os
import struct
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

import salmon.tagger.foldername
import salmon.trackers
import salmon.uploader

RENAMED = "Artist - Album (2020) [WEB FLAC]"


class FakeSite:
    """A tracker that answers torrentgroup from memory and counts the calls."""

    site_code = "RED"
    site_string = "RED"
    base_url = "https://tracker.test"

    def __init__(self, group: dict[str, Any]) -> None:
        self.group = group
        self.torrentgroup_calls = 0

    async def torrentgroup(self, group_id: int) -> dict[str, Any]:
        self.torrentgroup_calls += 1
        return self.group


def _group() -> dict[str, Any]:
    flac = {
        "id": 11,
        "media": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "remastered": True,
        "remasterYear": 2020,
        "remasterTitle": "",
        "remasterRecordLabel": "Label",
        "remasterCatalogueNumber": "CAT1",
    }
    return {
        "group": {
            "id": 5,
            "name": "Album",
            "year": 2020,
            "recordLabel": "Label",
            "catalogueNumber": "",
            "musicInfo": {"artists": [{"name": "Artist"}]},
        },
        "torrents": [flac],
    }


def _write_flac(path: Path, **tags: str) -> None:
    """Write a FLAC file with no audio: a STREAMINFO block and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()


def _release(folder: Path) -> Path:
    folder.mkdir(parents=True)
    # "year" is an alias standardize_tags rewrites to "date", in place.
    _write_flac(folder / "01 - one.flac", title="One", artist="Artist", year="2020")
    _write_flac(folder / "02 - two.flac", title="Two", artist="Artist", year="2020")
    (folder / "cover.jpg").write_bytes(b"jpeg")
    # Old mtimes, so a rewrite shows even within the filesystem's timestamp resolution.
    for entry in folder.iterdir():
        os.utime(entry, ns=(1_000_000_000, 1_000_000_000))
    return folder


def _snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under folder: its bytes and mtime, by relative path."""
    return {
        str(file.relative_to(folder)): (file.read_bytes(), file.stat().st_mtime_ns)
        for file in sorted(folder.rglob("*"))
        if file.is_file()
    }


def _retag(path: str, *_args: Any) -> None:
    """Stands in for tag_files: rewrites a tag in every FLAC of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            tagged = FLAC(os.path.join(path, name))
            tagged["album"] = "Album"
            tagged.save()


def _rename_files(path: str, *_args: Any) -> None:
    """Stands in for rename_files: renames the tracks of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            os.rename(os.path.join(path, name), os.path.join(path, name.replace(" - ", ". ")))


def _returning(result: Any = None):
    def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _returning_async(result: Any = None):
    async def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


@pytest.fixture
def downloads(monkeypatch, tmp_path) -> Path:
    folder = tmp_path / "downloads"
    folder.mkdir()
    monkeypatch.setattr(salmon.uploader.cfg.directory, "download_directory", str(folder))
    monkeypatch.setattr(salmon.uploader.cfg.directory, "tmp_dir", None)
    return folder


def _run_up(
    monkeypatch, release: Path, remove_source_dir: bool = False, args: tuple[str, ...] = (), **fakes: Any
) -> tuple[Any, FakeSite, list[tuple[str, str]]]:
    """Run `salmon up RELEASE -g 5 --skip-flac-upload` with the real copy, tagging and folder rename.

    The seams that need audio tools, a network or a reviewer are stubbed. Returns the result, the fake
    site and the (source folder, output folder) of every transcode. `args` are added to the command
    line, and `fakes` replace more seams.
    """
    site = FakeSite(_group())
    transcodes: list[tuple[str, str]] = []

    async def fake_transcode(path: str, bitrate: str, *_args: Any, output_dir: str | None = None, **_kw: Any) -> str:
        new_path = os.path.join(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bitrate}]")
        os.makedirs(new_path)
        transcodes.append((path, new_path))
        return new_path

    class FakeUploadManager:
        async def execute_upload(self) -> None:
            pass

    rls_data = {
        "format": "FLAC",
        "encoding": "Lossless",
        "artists": [("Artist", "main")],
        "title": "Album",
        "catno": "CAT1",
    }
    metadata = {
        **rls_data,
        "source": "WEB",
        "year": 2020,
        "edition_title": None,
        "scene": False,
        "cover": None,
        "genres": [],
    }
    for name, fake in {
        "gather_audio_info": _returning({}),
        "check_hybrid": _returning(False),
        "gather_tags": _returning({}),
        "construct_rls_data": _returning(rls_data),
        "mqa_test": _returning_async(),
        "check_spectrals": _returning_async((False, None)),
        "get_metadata": _returning_async((metadata, None)),
        "review_metadata_with_ai": _returning_async(metadata),
        "tag_files": _retag,
        "check_tags": _returning_async({}),
        "rename_files": _rename_files,
        "check_folder_structure": _returning_async(),
        "concat_track_data": _returning({"01. one.flac": {"sample rate": 44100}}),
        "handle_spectrals_upload_and_deletion": _returning_async(),
        "resolve_cover_url": _returning_async((True, None)),
        "UploadManager": FakeUploadManager,
        "transcode_folder": fake_transcode,
        "upload_and_report": _returning_async((21, 5, "/t.torrent", b"", "https://tracker.test/t")),
        **fakes,
    }.items():
        monkeypatch.setattr(salmon.uploader, name, fake)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    monkeypatch.setattr(salmon.uploader.cfg.upload.formatting, "remove_source_dir", remove_source_dir)
    monkeypatch.setattr(salmon.uploader.cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(salmon.uploader.cfg.image, "auto_compress_cover", False)
    monkeypatch.setattr(salmon.trackers, "get_class", lambda _tracker: lambda: site)

    async def run():
        return await CliRunner().invoke(
            salmon.uploader.up,
            [str(release), "-t", "RED", "-g", "5", "-s", "WEB", "--skip-flac-upload", "-n", "-yyy"]
            + ["--skip-integrity-check", "--skip-up", *args],
            input="y\n",
        )

    return anyio.run(run), site, transcodes


@pytest.mark.parametrize("remove_source_dir", [False, True])
@pytest.mark.parametrize("where", ["elsewhere", "in downloads, named as the rename would name it"])
def test_the_source_folder_is_left_byte_identical(
    monkeypatch, tmp_path, downloads, where: str, remove_source_dir: bool
) -> None:
    release = _release(downloads / RENAMED if where.startswith("in downloads") else tmp_path / "seeding" / "Album")
    before = _snapshot(release)

    result, site, transcodes = _run_up(monkeypatch, release, remove_source_dir)

    assert result.exit_code == 0, result.output
    assert _snapshot(release) == before
    # Both MP3 formats were transcoded, from the renamed copy, into download_directory.
    assert [os.path.dirname(output) for _source, output in transcodes] == [str(downloads)] * 2
    assert all(os.path.basename(source) == RENAMED for source, _output in transcodes)
    assert all(source != str(release) for source, _output in transcodes)
    # Nothing but the transcodes is left behind: the scratch copy and its renamed folder are gone.
    assert os.listdir(downloads / ".salmon-staging") == []
    left = {".salmon-staging", f"{RENAMED} [320]", f"{RENAMED} [V0]"} | ({RENAMED} if where != "elsewhere" else set())
    assert set(os.listdir(downloads)) == left
    # The flag adds no tracker request: the group comes from the -g confirmation.
    assert site.torrentgroup_calls == 1


def test_an_aborted_run_leaves_the_source_and_removes_the_copy(monkeypatch, tmp_path, downloads) -> None:
    release = _release(tmp_path / "seeding" / "Album")
    before = _snapshot(release)

    async def abort(*_args: Any, **_kwargs: Any) -> None:
        raise click.Abort

    result, _site, transcodes = _run_up(monkeypatch, release, review_metadata_with_ai=abort)

    assert result.exit_code == 0, result.output
    assert "Aborting upload" in result.output
    assert transcodes == []
    assert _snapshot(release) == before
    assert os.listdir(downloads) == [".salmon-staging"]
    assert os.listdir(downloads / ".salmon-staging") == []


def test_a_run_that_fails_leaves_the_source_and_removes_the_copy(monkeypatch, tmp_path, downloads) -> None:
    release = _release(tmp_path / "seeding" / "Album")
    before = _snapshot(release)

    async def crash(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("transcoder crashed")

    result, _site, _transcodes = _run_up(monkeypatch, release, transcode_folder=crash)

    assert isinstance(result.exception, RuntimeError)
    assert _snapshot(release) == before
    assert os.listdir(downloads / ".salmon-staging") == []


def _embed_front_cover(path: Path, size: int) -> None:
    audio = FLAC(path)
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = "image/jpeg"
    picture.data = b"x" * size
    audio.add_picture(picture)
    audio.save()


@pytest.mark.parametrize(
    ("strip", "args", "stripped"),
    [
        (False, (), False),
        (True, (), True),
        (True, ("--scene",), False),
    ],
    ids=["default", "strip_oversized_pictures", "scene"],
)
def test_oversized_pictures_are_stripped_from_the_copy_only_when_asked(
    monkeypatch, tmp_path, downloads, strip: bool, args: tuple[str, ...], stripped: bool
) -> None:
    release = _release(tmp_path / "seeding" / "Album")
    _embed_front_cover(release / "01 - one.flac", 1024 * 1024)
    before = _snapshot(release)
    monkeypatch.setattr(salmon.uploader.cfg.image, "strip_oversized_pictures", strip)
    pictures_when_transcoded: list[int] = []

    async def transcode(path: str, bitrate: str, *_args: Any, output_dir: str | None = None, **_kw: Any) -> str:
        pictures_when_transcoded.append(len(FLAC(os.path.join(path, "01. one.flac")).pictures))
        new_path = os.path.join(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bitrate}]")
        os.makedirs(new_path)
        return new_path

    result, _site, _transcodes = _run_up(monkeypatch, release, args=args, transcode_folder=transcode)

    assert result.exit_code == 0, result.output
    assert ("RED does not allow (rule 2.3.19)" in result.output) is not ("--scene" in args)
    assert pictures_when_transcoded == [0 if stripped else 1] * 2
    assert _snapshot(release) == before
