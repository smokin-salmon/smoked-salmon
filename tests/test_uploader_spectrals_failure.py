"""Spectrals that could not be uploaded are kept, and their host is not tried again in the run (#1019).

The image hosts are fakes: nothing is sent anywhere.
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import anyio
import pytest

import salmon.images
from salmon import cfg, dryrun, interaction
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader
from salmon.images.files import NotAnImage
from salmon.uploader import spectrals
from salmon.uploader.spectrals import (
    SpectralsDeclined,
    SpectralsNotUploaded,
    SpectralUploads,
    create_specs_folder,
    get_spectrals_path,
    made_by_salmon,
)

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

SPECTRALS = ("01 Full.png", "01 Zoom.png")
# Only its code and name are read.
RED = cast("BaseGazelleApi", cast(object, SimpleNamespace(site_code="RED", site_string="RED")))
IDS = {1: "01. one.flac"}


@pytest.fixture
def scratch(monkeypatch, tmp_path) -> Path:
    """The system temporary directory, where a run moves spectrals out of the album: empty, and watched."""
    folder = tmp_path / "system-tmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


@pytest.fixture
def tries(monkeypatch) -> list[str]:
    """A specs host that is down: each try at it is noted."""
    noted: list[str] = []

    def down() -> BaseImageUploader:
        noted.append("tried")
        raise ImageUploadFailed("the host is down")

    monkeypatch.setitem(salmon.images.HOSTS, "testhost", SimpleNamespace(ImageUploader=down))
    monkeypatch.setattr(cfg.image, "specs_uploader", "testhost")
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return noted


def _album(tmp_path: Path) -> Path:
    album = tmp_path / "Album"
    album.mkdir()
    specs = Path(create_specs_folder(str(album)))
    for name in SPECTRALS:
        (specs / name).write_bytes(b"png")
    return album


def _fail(uploads: SpectralUploads, tracker: str = "RED") -> SpectralsNotUploaded:
    with pytest.raises(SpectralsNotUploaded) as failure:
        anyio.run(uploads.urls_for, tracker, IDS)
    return failure.value


def test_a_failed_upload_keeps_the_spectrals_out_of_the_album_and_close_keeps_them(
    tmp_path, scratch, tries, capsys
) -> None:
    album = _album(tmp_path)
    uploads = SpectralUploads(str(album), ["RED"])

    failure = _fail(uploads)

    kept = next(scratch.glob("salmon-spectrals-*/Spectrals"))
    assert sorted(os.listdir(kept)) == sorted(SPECTRALS)
    # Out of the album, so that no torrent the run goes on to make holds them.
    assert not (album / "Spectrals").exists()
    assert str(failure) == f"The spectrals could not be uploaded to testhost. They are kept in {kept}."

    anyio.run(uploads.close)

    assert sorted(os.listdir(kept)) == sorted(SPECTRALS)
    assert f"The spectrals are kept in {kept}: they could not be uploaded to testhost." in capsys.readouterr().out
    # salmon's by their files, as a later run on the album finds them.
    assert made_by_salmon(str(kept))


def test_spectrals_made_outside_the_album_are_kept_where_they_are(monkeypatch, tmp_path, scratch, tries) -> None:
    tmp_dir = tmp_path / "tmp_dir"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    album = tmp_path / "Album"
    album.mkdir()
    specs = Path(create_specs_folder(str(album)))
    assert specs.parent == tmp_dir
    for name in SPECTRALS:
        (specs / name).write_bytes(b"png")
    uploads = SpectralUploads(str(album), ["RED"])

    _fail(uploads)
    anyio.run(uploads.close)

    assert sorted(os.listdir(specs)) == sorted(SPECTRALS)
    assert list(scratch.iterdir()) == []


def test_a_failed_host_is_not_tried_again_by_a_tracker_sharing_it(tmp_path, scratch, tries) -> None:
    uploads = SpectralUploads(str(_album(tmp_path)), ["RED", "OPS"])

    _fail(uploads, "RED")
    _fail(uploads, "OPS")

    assert tries == ["tried"]


def test_the_first_trackers_turn_is_not_asked_again_after_a_yes(monkeypatch, tmp_path, scratch, tries) -> None:
    asked: list[str] = []

    async def yes(text: str, **_kwargs) -> bool:
        asked.append(text)
        return True

    monkeypatch.setattr(interaction, "confirm", yes)
    uploads = SpectralUploads(str(_album(tmp_path)), ["RED"])
    first = anyio.run(uploads.urls_for_upload, RED, IDS)
    again = anyio.run(uploads.urls_for_upload, RED, IDS)

    assert (first, again) == (None, None)
    assert len(asked) == 1
    assert "Upload to RED without spectrals?" in asked[0]
    assert not uploads.declined("RED")


@pytest.mark.parametrize("yes_all", [False, True], ids=["answered no", "-yyy"])
def test_no_is_the_default_and_the_answer_with_yyy(monkeypatch, tmp_path, scratch, tries, yes_all) -> None:
    asked: list[bool | None] = []

    async def default(_text: str, *, default: bool | None = False, **_kwargs) -> bool:
        asked.append(default)
        return bool(default)

    monkeypatch.setattr(interaction, "confirm", default)
    monkeypatch.setattr(cfg.upload, "yes_all", yes_all)
    uploads = SpectralUploads(str(_album(tmp_path)), ["RED"])

    with pytest.raises(SpectralsDeclined, match="Not uploading to RED without its spectrals"):
        anyio.run(uploads.urls_for_upload, RED, IDS)

    assert asked == ([] if yes_all else [False])
    assert uploads.declined("RED")


def test_a_dry_run_keeps_nothing(monkeypatch, tmp_path, scratch, tries) -> None:
    def refused(_path: str) -> str:
        raise NotAnImage("it is cut short")

    # A dry run uploads nothing: only a spectral that is not an image fails it.
    monkeypatch.setattr(spectrals, "check_image", refused)
    album = _album(tmp_path)
    with dryrun.mode():
        uploads = SpectralUploads(str(album), ["RED"])
        failure = _fail(uploads)
        anyio.run(uploads.close)

    assert "kept" not in str(failure)
    assert not os.path.exists(get_spectrals_path(str(album)))
    assert [path for path in scratch.rglob("*") if path.is_file()] == []
