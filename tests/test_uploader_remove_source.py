"""salmon up removes the source folder only once the run ends, never at the folder rename."""

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import asyncclick as click
import pytest

import salmon.uploader as uploader
from salmon import cfg, interaction
from salmon.converter import conversions
from salmon.tagger import foldername

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

NEW_NAME = "Artist - Album (2020) [WEB FLAC]"
METADATA: dict[str, Any] = {
    "scene": False,
    "artists": [("Artist", "main")],
    "title": "Album",
    "year": 2020,
    "source": "WEB",
    "format": "FLAC",
    "encoding": "Lossless",
}


@pytest.fixture
def album(monkeypatch, tmp_path) -> Path:
    """An album outside download_directory, with remove_source_dir on and no library."""
    downloads, inbox = tmp_path / "downloads", tmp_path / "inbox"
    downloads.mkdir()
    folder = inbox / "Album"
    folder.mkdir(parents=True)
    (folder / "01 - one.flac").write_bytes(b"one")
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    monkeypatch.setattr(foldername, "generate_folder_name", lambda _metadata: NEW_NAME)
    return folder


def _staged_run(monkeypatch, *, abort: bool = False, finished: bool = True) -> None:
    """Stand in for the run: rename the folder as the metadata step does, then abort or say whether it finished."""

    async def run(_site, path, *_args, **_kwargs):
        renamed = await foldername.rename_folder(path, METADATA, auto_rename=True, check=False)
        if abort:
            raise click.Abort
        return renamed, finished

    monkeypatch.setattr(uploader, "_upload_staged", run)


def _up(album: Path) -> None:
    with interaction.assuming_defaults():
        anyio.run(lambda: uploader.upload(cast("BaseGazelleApi", object()), str(album), None, "WEB", False, (), None))


def test_an_abort_after_the_rename_keeps_the_source_folder(monkeypatch, album) -> None:
    _staged_run(monkeypatch, abort=True)

    with pytest.raises(click.Abort):
        _up(album)

    assert (album / "01 - one.flac").read_bytes() == b"one"


def test_a_run_that_uploaded_removes_the_source_folder(monkeypatch, album) -> None:
    _staged_run(monkeypatch)

    _up(album)

    assert not album.exists()
    assert (Path(cfg.directory.download_directory) / NEW_NAME / "01 - one.flac").read_bytes() == b"one"


def test_a_run_that_uploaded_nothing_keeps_the_source_folder(monkeypatch, album, capsys) -> None:
    _staged_run(monkeypatch, finished=False)

    _up(album)

    assert album.exists()
    assert "the run did not finish" in capsys.readouterr().out


def test_a_metadata_rerun_removes_only_salmons_own_folder(monkeypatch, album) -> None:
    downloads = Path(cfg.directory.download_directory)
    names = iter(["Artist - Album (2020) [CD FLAC]", "Artist - Album (2021) [CD FLAC]"])
    sources: list[str | None] = []
    answers = iter([False, True])  # No to "upload the torrent?" re-runs the metadata step once.

    async def returns(value=None):
        return value

    async def rename_folder(path, *_args, **_kwargs):
        new = downloads / next(names)
        new.mkdir()
        # As the real rename does: the record is copied, and the old one stays while its folder does.
        conversions.record_conversion(str(new), source="/x/src", kind="downconvert", bit_depth=16, sample_rate=44100)
        return str(new)

    async def rename_files(*args, **_kwargs):
        sources.append(args[5])

    async def confirm(*_args, **_kwargs):
        return next(answers)

    monkeypatch.setattr(uploader, "review_metadata_with_ai", lambda metadata, *a, **k: returns(metadata))
    monkeypatch.setattr(uploader, "tag_files", lambda *a, **k: returns())
    monkeypatch.setattr(uploader, "check_tags", lambda *a, **k: returns({}))
    monkeypatch.setattr(uploader, "rename_folder", rename_folder)
    monkeypatch.setattr(uploader, "rename_files", rename_files)
    monkeypatch.setattr(uploader, "check_folder_structure", lambda *a, **k: returns())
    monkeypatch.setattr(uploader, "gather_tags", lambda *a, **k: {})
    monkeypatch.setattr(uploader, "gather_audio_info", lambda *a, **k: {})
    monkeypatch.setattr(interaction, "confirm", confirm)
    metadata = {"scene": False, "genres": []}

    path, *_ = anyio.run(
        lambda: uploader.edit_metadata(str(album), {}, metadata, None, "CD", {}, False, False, None, True)
    )

    assert path == str(downloads / "Artist - Album (2021) [CD FLAC]")
    assert not (downloads / "Artist - Album (2020) [CD FLAC]").exists()
    assert conversions.conversion_of(str(downloads / "Artist - Album (2020) [CD FLAC]")) is None
    assert conversions.conversion_of(path) is not None
    assert album.exists()
    assert sources == ["CD", "CD"]
