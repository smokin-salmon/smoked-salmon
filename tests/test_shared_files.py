"""An album that shares files through a hardlink or symlink is never rewritten in place: what it shares stays as it is.

The case: a release a torrent client seeds, hardlinked (or symlinked) into another folder that salmon is run on. A tag
write goes into the shared inode, or through the symlink, so retagging that folder in place would change the seeded
files and fail the torrent's recheck. No library_dirs is configured here: the rule holds without one.
"""

import os
import re
import sys
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC
from test_library_dirs import (  # pyright: ignore[reportMissingImports]
    RENAMED,
    _album,
    _inodes,
    _metadata,
    _returning,
    _returning_async,
    _run_tag,
    _run_up,
    _snapshot,
)

import salmon.checks
import salmon.commands
import salmon.tagger.foldername
from salmon import cfg, interaction
from salmon.common.files import AlbumPath, rewrite_refusal, shares_files
from salmon.config import validations
from salmon.errors import AbortAndDeleteFolder, UploadError
from salmon.uploader import staging

SHARING = ["hardlinked file", "symlinked file", "symlinked folder"]


@pytest.fixture
def seeded(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory, and the folder a torrent client seeds from beside it; no library_dirs."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    seeding = tmp_path / "seeding"
    seeding.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return downloads, seeding


def _share(album: Path, seeding: Path, how: str) -> Path:
    """Share part of album with a seeded copy of it, the way `how` says; return the seeded folder."""
    seeded = seeding / "Album"
    seeded.mkdir()
    try:
        if how == "hardlinked file":
            os.link(album / "01 - one.flac", seeded / "01 - one.flac")
        elif how == "symlinked file":
            (album / "01 - one.flac").rename(seeded / "01 - one.flac")
            (album / "01 - one.flac").symlink_to(seeded / "01 - one.flac")
        else:
            (album / "CD1").rename(seeded / "CD1")
            (album / "CD1").symlink_to(seeded / "CD1", target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"this filesystem refuses the link: {error}")
    return seeded


def _symlinked_album(downloads: Path, seeding: Path) -> Path:
    """An album folder that is itself a symlink to the seeded folder; return the link."""
    seeded = _album(seeding / "Album")
    link = downloads / "Album"
    try:
        link.symlink_to(seeded, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"this filesystem refuses symlinks: {error}")
    return link


# shares_files and rewrite_refusal


@pytest.mark.parametrize("how", SHARING)
def test_an_album_sharing_a_file_or_folder_shares_files(seeded, how: str) -> None:
    downloads, seeding = seeded
    album = _album(downloads / "Album")
    _share(album, seeding, how)

    assert shares_files(str(album))
    assert rewrite_refusal(str(album)) == "it shares files with another folder through a hardlink or symlink"


def test_an_album_that_is_a_symlink_shares_files(seeded) -> None:
    link = _symlinked_album(*seeded)

    assert shares_files(str(link))
    assert not shares_files(os.path.realpath(link))


def test_an_album_with_no_link_shares_nothing(seeded) -> None:
    downloads, _seeding = seeded
    album = _album(downloads / "Album")

    assert not shares_files(str(album))
    assert rewrite_refusal(str(album)) is None


def test_a_file_with_a_second_hardlink_shares_files(seeded) -> None:
    downloads, seeding = seeded
    album = _album(downloads / "Album")
    _share(album, seeding, "hardlinked file")

    assert shares_files(str(album / "01 - one.flac"))
    assert not shares_files(str(album / "CD1" / "02 - two.flac"))


def test_an_unreadable_folder_counts_as_sharing_files(seeded) -> None:
    downloads, _seeding = seeded
    album = _album(downloads / "Album")
    (album / "CD1").chmod(0)
    try:
        if os.access(album / "CD1", os.R_OK):
            pytest.skip("permission bits are not enforced for this user")
        assert shares_files(str(album))
    finally:
        (album / "CD1").chmod(0o755)


def test_an_entry_that_vanishes_mid_walk_counts_as_sharing_files(monkeypatch, seeded) -> None:
    downloads, _seeding = seeded
    album = _album(downloads / "Album")
    real_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(path).endswith("cover.jpg"):
            raise FileNotFoundError(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)

    assert shares_files(str(album))


# A path matched to library_dirs by device and inode


def test_a_library_given_in_another_case_is_matched_on_a_case_insensitive_volume(monkeypatch, tmp_path) -> None:
    library = tmp_path / "Music"
    (library / "Artist" / "Album").mkdir(parents=True)
    other_case = tmp_path / "music"
    if not other_case.is_dir():
        # A case-sensitive filesystem: os.stat answers for the other case as a case-insensitive volume does.
        real_stat = os.stat

        def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            text = os.fspath(path)
            if text == str(other_case) or text.startswith(str(other_case) + os.sep):
                path = str(library) + text[len(str(other_case)) :]
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])

    assert cfg.directory.is_library_path(str(other_case / "Artist" / "Album"))
    assert cfg.directory.library_inside(str(other_case)) == str(library)
    assert not cfg.directory.is_library_path(str(tmp_path))


def test_a_library_reached_by_a_path_no_text_matches_is_matched(monkeypatch, tmp_path) -> None:
    library = tmp_path / "music"
    (library / "Album").mkdir(parents=True)
    (tmp_path / "mount").symlink_to(library, target_is_directory=True)
    # Stands in for a second bind mount: the text names another folder, the device and inode are the library's.
    monkeypatch.setattr(validations, "_real", lambda path: os.path.abspath(os.path.expanduser(path)))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])

    assert cfg.directory.is_library_path(str(tmp_path / "mount" / "Album"))
    assert not cfg.directory.is_library_path(str(tmp_path))


# AlbumPath


def test_an_album_path_resolves_all_but_a_last_symlink(seeded, tmp_path, monkeypatch) -> None:
    downloads, seeding = seeded
    link = _symlinked_album(downloads, seeding)
    (tmp_path / "shortcut").symlink_to(downloads, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    given = AlbumPath(exists=True).convert(os.path.join("shortcut", "Album"), None, None)
    assert given == os.path.join(os.path.realpath(downloads), link.name)
    assert os.path.islink(given)
    # The filesystem takes "link/.." from where the link leads, so the path does too.
    walked = AlbumPath(exists=True).convert(os.path.join("shortcut", "Album", "..", "Album"), None, None)
    assert walked == os.path.realpath(seeding / "Album")
    assert AlbumPath(exists=True).convert(os.path.join("shortcut", "."), None, None) == os.path.realpath(downloads)


# staged_source


@pytest.mark.parametrize("how", SHARING)
def test_an_album_sharing_files_is_staged_as_a_real_copy(seeded, capsys, how: str) -> None:
    downloads, seeding = seeded
    album = _album(downloads / "Album")
    _share(album, seeding, how)
    before = _snapshot(seeding)

    with staging.staged_source(str(album), scratch=False) as (staged, rename_into):
        assert rename_into is None
        assert os.path.dirname(os.path.dirname(staged)) == str(downloads / staging.STAGING_DIR)
        assert not any(entry.is_symlink() for entry in Path(staged).rglob("*"))
        assert _inodes(Path(staged)).isdisjoint(_inodes(seeding))
        for flac in Path(staged).rglob("*.flac"):
            flac.write_bytes(b"changed")

    assert _snapshot(seeding) == before
    assert os.listdir(downloads / staging.STAGING_DIR) == []
    out = capsys.readouterr().out
    assert "It shares files with another folder through a hardlink or symlink, so salmon works on a copy" in out


def test_an_album_sharing_no_file_is_worked_on_in_place(seeded) -> None:
    downloads, _seeding = seeded
    album = _album(downloads / "Album")

    with staging.staged_source(str(album), scratch=False) as (staged, rename_into):
        assert (staged, rename_into) == (str(album), None)


def test_a_library_folder_itself_is_refused(monkeypatch, seeded, tmp_path) -> None:
    library = tmp_path / "music"
    _album(library / "Album")
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])

    with (
        pytest.raises(UploadError, match="holds the library folder"),
        staging.staged_source(str(library), scratch=False),
    ):
        pytest.fail("the upload ran on a whole library")


# salmon up


@pytest.mark.parametrize("how", SHARING)
def test_up_leaves_an_album_sharing_files_and_what_it_shares_byte_identical(monkeypatch, seeded, how: str) -> None:
    downloads, seeding = seeded
    album = _album(downloads / "Album")
    _share(album, seeding, how)
    before = _snapshot(seeding), _snapshot(album)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)

    result, uploads, _transcodes = _run_up(monkeypatch, album)

    assert result.exit_code == 0, result.output
    assert (_snapshot(seeding), _snapshot(album)) == before
    copy = downloads / RENAMED
    assert uploads[0] == str(copy)
    assert _inodes(copy).isdisjoint(_inodes(seeding))
    # The run's changes are in the copy.
    assert FLAC(copy / "01. one.flac")["album"] == ["Album"]
    assert f"remove_source_dir is on, but {album} is kept: it shares files" in result.output


def test_up_on_an_album_folder_that_is_a_symlink_leaves_its_target_byte_identical(monkeypatch, seeded) -> None:
    downloads, seeding = seeded
    link = _symlinked_album(downloads, seeding)
    before = _snapshot(seeding)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)

    result, uploads, _transcodes = _run_up(monkeypatch, link)

    assert result.exit_code == 0, result.output
    assert _snapshot(seeding) == before
    assert link.is_symlink()
    assert uploads[0] == str(downloads / RENAMED)


@pytest.mark.parametrize("how", SHARING)
def test_up_never_replaces_an_album_sharing_files_that_already_has_the_new_name(monkeypatch, seeded, how: str) -> None:
    downloads, seeding = seeded
    album = _album(downloads / RENAMED)
    _share(album, seeding, how)
    before = _snapshot(seeding), _snapshot(album), _inodes(album)

    result, uploads, _transcodes = _run_up(monkeypatch, album)

    assert (_snapshot(seeding), _snapshot(album), _inodes(album)) == before
    assert uploads == []
    assert str(result.exception) == (
        f"Not replacing {album}: it is the album this run works on a copy of. "
        "Rename it, or give this album another name. "
        "If it is already uploaded, salmon cross-upload sends it to another tracker."
    )
    assert os.listdir(downloads / staging.STAGING_DIR) == []


def test_up_on_an_album_with_no_link_works_on_it_in_place(monkeypatch, seeded) -> None:
    downloads, _seeding = seeded
    album = _album(downloads / "Album")
    monkeypatch.setattr(cfg.directory, "hardlinks", False)

    result, uploads, _transcodes = _run_up(monkeypatch, album)

    assert result.exit_code == 0, result.output
    assert uploads[0] == str(downloads / RENAMED)
    assert "works on a copy" not in result.output
    # Worked on in place: the retag and the alias rewrite reached the album itself.
    assert FLAC(album / "01 - one.flac")["album"] == ["Album"]


def test_abort_and_delete_keeps_an_album_sharing_files_and_deletes_the_copy(monkeypatch, seeded) -> None:
    downloads, seeding = seeded
    album = _album(downloads / "Album")
    _share(album, seeding, "hardlinked file")
    before = _snapshot(seeding), _snapshot(album)

    async def delete(*_args: Any, **_kwargs: Any) -> None:
        raise AbortAndDeleteFolder

    monkeypatch.setattr(cfg.upload, "windows_use_recycle_bin", False)

    result, uploads, _transcodes = _run_up(monkeypatch, album, check_spectrals=delete)

    assert result.exit_code == 0, result.output
    assert uploads == []
    assert (_snapshot(seeding), _snapshot(album)) == before
    assert f"Deleting the copy the upload worked on. {album} is kept." in result.output
    assert "Deleted folder" in result.output
    assert sorted(os.listdir(downloads)) == sorted([staging.STAGING_DIR, "Album"])


# salmon tag


@pytest.mark.parametrize("how", SHARING)
def test_tag_leaves_an_album_sharing_files_and_what_it_shares_byte_identical(monkeypatch, seeded, how: str) -> None:
    downloads, seeding = seeded
    album = _album(downloads / "Album")
    _share(album, seeding, how)
    before = _snapshot(seeding), _snapshot(album)

    result = _run_tag(monkeypatch, album)

    assert result.exit_code == 0, result.output
    assert (_snapshot(seeding), _snapshot(album)) == before
    assert set(os.listdir(downloads)) == {staging.STAGING_DIR, "Album", RENAMED}
    assert f"remove_source_dir is on, but {album} is kept: it shares files" in result.output
    assert FLAC(downloads / RENAMED / "01. one.flac")["album"] == ["Album"]


# rename_folder


def test_rename_folder_never_replaces_the_album_it_renames_a_copy_of(monkeypatch, seeded, tmp_path) -> None:
    downloads, seeding = seeded
    original = _album(downloads / RENAMED)
    _share(original, seeding, "hardlinked file")
    before = _snapshot(original), _inodes(original)
    copy = _album(tmp_path / "run" / "Album")
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))

    with pytest.raises(UploadError, match=re.escape(f"Not replacing {original}: it is the album this run works on")):
        anyio.run(
            lambda: salmon.tagger.foldername.rename_folder(
                str(copy), _metadata(), auto_rename=True, check=False, original=str(original)
            )
        )

    assert (_snapshot(original), _inodes(original)) == before


@pytest.mark.parametrize("how", SHARING)
def test_rename_folder_never_replaces_an_earlier_folder_of_that_name_that_shares_files(
    monkeypatch, seeded, how: str
) -> None:
    # An earlier run's renamed folder shares its files with that run's source when hardlinks is on, and a torrent
    # may seed from it.
    downloads, seeding = seeded
    earlier = _album(downloads / RENAMED)
    _share(earlier, seeding, how)
    before = sorted(os.listdir(earlier))
    source = _album(seeding / "Other")
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    monkeypatch.setattr(cfg.directory, "hardlinks", False)

    with pytest.raises(UploadError, match=re.escape(f"Not replacing {earlier}: {rewrite_refusal(str(earlier))}")):
        anyio.run(
            lambda: salmon.tagger.foldername.rename_folder(str(source), _metadata(), auto_rename=True, check=False)
        )

    assert sorted(os.listdir(earlier)) == before
    assert os.path.isdir(source)


@pytest.mark.parametrize(("answer", "replaced"), [(None, False), (True, True)], ids=["default", "told_yes"])
def test_rename_folder_replaces_an_earlier_folder_of_that_name_only_when_told_to(
    monkeypatch, seeded, answer: bool | None, replaced: bool
) -> None:
    downloads, seeding = seeded
    earlier = _album(downloads / RENAMED)
    (earlier / "notes.txt").write_text("an earlier upload")
    source = _album(seeding / "Other")
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    monkeypatch.setattr(cfg.directory, "hardlinks", False)
    asked: list[str] = []

    async def confirm(text: str, *, default: bool | None = False, **_kwargs: Any) -> bool | None:
        asked.append(click.unstyle(text))
        return default if answer is None else answer

    monkeypatch.setattr(interaction, "confirm", confirm)

    def rename() -> str:
        return anyio.run(lambda: salmon.tagger.foldername.rename_folder(str(source), _metadata(), auto_rename=True))

    if replaced:
        assert rename() == str(earlier)
        assert not (earlier / "notes.txt").exists()
    else:
        with pytest.raises(UploadError, match="New folder name already exists"):
            rename()
        assert (earlier / "notes.txt").read_text() == "an earlier upload"
    assert any("would you like to replace it?" in question for question in asked)


# Commands that rewrite a folder in place


@pytest.mark.parametrize("how", [*SHARING, "symlinked album"])
def test_compress_refuses_an_album_sharing_files(monkeypatch, seeded, how: str) -> None:
    downloads, seeding = seeded
    if how == "symlinked album":
        album = _symlinked_album(downloads, seeding)
    else:
        album = _album(downloads / "Album")
        _share(album, seeding, how)
    recompressed: list[str] = []

    async def fake_recompress_flacs(path: str) -> None:
        recompressed.append(path)

    monkeypatch.setattr(salmon.commands, "recompress_flacs", fake_recompress_flacs)

    async def run():
        return await CliRunner().invoke(salmon.commands.compress, [str(album)])

    result = anyio.run(run)

    assert result.exit_code != 0
    assert f"Not recompressing {album}: it shares files with another folder through a hardlink or symlink" in (
        result.output
    )
    assert recompressed == []


@pytest.mark.parametrize("target", ["folder", "hardlinked file", "symlinked file"])
def test_check_integrity_does_not_offer_to_sanitize_an_album_sharing_files(monkeypatch, seeded, target: str) -> None:
    downloads, seeding = seeded
    album = _album(downloads / "Album")
    if target == "symlinked file":
        _share(album, seeding, "symlinked file")
        path = album / "01 - one.flac"
    else:
        _share(album, seeding, "hardlinked file")
        path = album if target == "folder" else album / "01 - one.flac"
    sanitized: list[str] = []

    async def sanitize(path: str) -> None:
        sanitized.append(path)

    integrity = sys.modules["salmon.checks.integrity"]
    monkeypatch.setattr(integrity, "check_integrity", _returning_async(integrity.IntegrityResult(passed=False)))
    monkeypatch.setattr(integrity, "sanitize_and_verify", sanitize)

    async def run():
        return await CliRunner().invoke(salmon.checks.check, ["integrity", str(path)], input="y\n")

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert f"Not offering to sanitize {path}: it shares files" in result.output
    assert sanitized == []


def test_check_integrity_still_offers_to_sanitize_an_album_with_no_link(monkeypatch, seeded) -> None:
    downloads, _seeding = seeded
    album = _album(downloads / "Album")
    sanitized: list[str] = []

    async def sanitize(path: str) -> None:
        sanitized.append(path)

    integrity = sys.modules["salmon.checks.integrity"]
    monkeypatch.setattr(integrity, "check_integrity", _returning_async(integrity.IntegrityResult(passed=False)))
    monkeypatch.setattr(integrity, "sanitize_and_verify", sanitize)

    async def run():
        return await CliRunner().invoke(salmon.checks.check, ["integrity", str(album)], input="y\n")

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert sanitized == [str(album)]
