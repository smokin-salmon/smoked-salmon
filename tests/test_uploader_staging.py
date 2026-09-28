import errno
import os
from pathlib import Path
from types import SimpleNamespace

import asyncclick as click
import pytest

from salmon.errors import UploadError
from salmon.uploader import staging


@pytest.fixture
def downloads(monkeypatch, tmp_path) -> Path:
    folder = tmp_path / "downloads"
    folder.mkdir()
    monkeypatch.setattr(staging.cfg.directory, "download_directory", str(folder))
    return folder


@pytest.fixture
def source(tmp_path) -> Path:
    folder = tmp_path / "seeding" / "Album"
    (folder / "CD1").mkdir(parents=True)
    (folder / "CD1" / "01.flac").write_bytes(b"flac")
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


def _files(folder: Path) -> dict[str, bytes]:
    return {str(f.relative_to(folder)): f.read_bytes() for f in sorted(folder.rglob("*")) if f.is_file()}


def test_without_scratch_the_upload_works_on_the_source(downloads, source) -> None:
    with staging.staged_source(str(source), scratch=False) as (staged, rename_into):
        assert (staged, rename_into) == (str(source), None)

    assert os.listdir(downloads) == []


def test_the_scratch_copy_is_a_real_copy_in_a_run_directory_removed_on_success(downloads, source, capsys) -> None:
    before = _files(source)

    with staging.staged_source(str(source), scratch=True) as (staged, rename_into):
        assert rename_into is not None
        assert os.path.dirname(rename_into) == str(downloads / staging.STAGING_DIR)
        assert staged == os.path.join(rename_into, "Album")
        assert _files(Path(staged)) == before
        # Not a hardlink: writing to the copy leaves the source alone.
        assert os.stat(os.path.join(staged, "cover.jpg")).st_ino != os.stat(source / "cover.jpg").st_ino
        Path(staged, "cover.jpg").write_bytes(b"changed")

    assert os.listdir(downloads / staging.STAGING_DIR) == []
    assert _files(source) == before
    out = capsys.readouterr().out
    assert f"Copying {source}" in out
    assert staged in out


@pytest.mark.parametrize("error", [click.Abort, RuntimeError, KeyboardInterrupt])
def test_the_scratch_copy_is_removed_on_abort_and_error(downloads, source, error: type[BaseException]) -> None:
    with pytest.raises(error), staging.staged_source(str(source), scratch=True):
        raise error

    assert os.listdir(downloads / staging.STAGING_DIR) == []
    assert _files(source) == {"CD1/01.flac": b"flac", "cover.jpg": b"jpeg"}


def test_a_leftover_from_an_earlier_run_is_neither_reused_nor_removed(downloads, source) -> None:
    leftover = downloads / staging.STAGING_DIR / "run-earlier"
    (leftover / "Album").mkdir(parents=True)
    (leftover / "Album" / "01.flac").write_bytes(b"old")

    with staging.staged_source(str(source), scratch=True) as (_staged, rename_into):
        assert rename_into != str(leftover)

    assert os.listdir(downloads / staging.STAGING_DIR) == ["run-earlier"]
    assert _files(leftover) == {"Album/01.flac": b"old"}


def test_a_copy_that_does_not_fit_is_refused_before_copying(monkeypatch, downloads, source) -> None:
    monkeypatch.setattr(staging.shutil, "disk_usage", lambda _path: SimpleNamespace(free=4))
    copies: list[str] = []
    monkeypatch.setattr(staging.shutil, "copytree", lambda src, *_a, **_k: copies.append(src))

    with pytest.raises(UploadError, match="only 0 MB free"), staging.staged_source(str(source), scratch=True):
        pytest.fail("the upload ran without a copy")

    assert copies == []
    assert os.listdir(downloads / staging.STAGING_DIR) == []


def test_a_failed_copy_is_refused_and_its_partial_copy_removed(monkeypatch, downloads, source) -> None:
    def copy_until_the_disk_is_full(src: str, dst: str, *args, **kwargs) -> None:
        os.makedirs(os.path.join(dst, "CD1"))
        Path(dst, "cover.jpg").write_bytes(b"jp")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(staging.shutil, "copytree", copy_until_the_disk_is_full)

    with pytest.raises(UploadError, match="No space left on device"), staging.staged_source(str(source), scratch=True):
        pytest.fail("the upload ran on a partial copy")

    assert os.listdir(downloads / staging.STAGING_DIR) == []
    assert _files(source) == {"CD1/01.flac": b"flac", "cover.jpg": b"jpeg"}


def test_removal_never_follows_a_symlink_out_of_the_staging_directory(downloads, source, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_bytes(b"keep")
    link = downloads / staging.STAGING_DIR / "run-link"
    link.parent.mkdir()
    link.symlink_to(elsewhere, target_is_directory=True)

    staging._remove_scratch_dir(str(link), str(source))

    assert link.is_symlink()
    assert _files(elsewhere) == {"keep": b"keep"}


def test_removal_never_touches_a_directory_outside_the_staging_directory(downloads, source) -> None:
    staging._remove_scratch_dir(str(source.parent), str(source))
    staging._remove_scratch_dir(str(downloads), str(source))

    assert _files(source) == {"CD1/01.flac": b"flac", "cover.jpg": b"jpeg"}


def test_removal_never_removes_a_run_directory_holding_the_source(downloads) -> None:
    run = downloads / staging.STAGING_DIR / "run-x"
    (run / "Album").mkdir(parents=True)
    (run / "Album" / "01.flac").write_bytes(b"flac")

    staging._remove_scratch_dir(str(run), str(run / "Album"))

    assert _files(run) == {"Album/01.flac": b"flac"}


def test_a_scratch_copy_that_cannot_be_removed_is_reported(monkeypatch, downloads, source, capsys) -> None:
    def refuse(_path: str, *_args, **_kwargs) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    with staging.staged_source(str(source), scratch=True) as (_staged, rename_into):
        monkeypatch.setattr(staging.shutil, "rmtree", refuse)

    assert "Could not remove the scratch copy" in capsys.readouterr().out
    assert os.path.isdir(str(rename_into))
