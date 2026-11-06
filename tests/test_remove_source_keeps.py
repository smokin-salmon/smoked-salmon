"""remove_source_dir keeps the source folder when it holds files the renamed folder no longer has (#967).

The folder check deletes a file a tracker does not allow (booklet.zip, video.mkv) from the renamed folder, where the
source folder held the only other copy. These run `salmon up` through main, as tests/test_run_exit_status.py does,
but with the real check_folder_structure, and `salmon tag` as tests/helpers/library_dirs.py does.
"""

import io
import shutil
import sys
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from helpers.library_dirs import RENAMED, _run_tag
from helpers.library_dirs import _album as _tag_album
from helpers.uploader_dry_run import SOURCE_URL, _album, fake_upload_world
from test_run_exit_status import (
    _exit_of_main,
    _seedbox,
    no_waits,  # noqa: F401 (a fixture: see pytestmark)
)
from test_uploader_dry_run import image_uploads  # noqa: F401 (a fixture: see pytestmark)

from salmon import cfg
from salmon.tagger.foldername import remove_source_folder
from salmon.tagger.folderstructure import check_folder_structure

pytestmark = pytest.mark.usefixtures("image_uploads", "no_waits")

REMOVED = "Removed the source folder"
EXTRAS = ("booklet.zip", "video.mkv")


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a dot_torrents_dir, configured, as in tests/test_run_exit_status.py."""
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return downloads, torrents


def _with_extras(album: Path, *names: str) -> Path:
    for name in names:
        (album / name).parent.mkdir(parents=True, exist_ok=True)
        (album / name).write_bytes(f"only here: {name}".encode())
    return album


def _up(
    monkeypatch,
    capsys,
    dirs,
    album: Path,
    *,
    yyy: bool = True,
    setup: Any = None,
    remove_source_dir: bool = True,
    check: Any = check_folder_structure,
) -> tuple[Any, str]:
    """Run `salmon up ALBUM -t RED` through main with the real folder check, remove_source_dir on unless told."""
    _downloads, torrents = dirs
    world, _queued, _logins = fake_upload_world(monkeypatch, torrents, check_folder_structure=check)
    if setup:
        setup(monkeypatch, album)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", remove_source_dir)
    argv = ["salmon", "up", str(album), "-t", "RED", "-s", "WEB", "-n", "--skip-integrity-check", "--skip-up"]
    monkeypatch.setattr(sys, "argv", [*argv, *(["-yyy"] if yyy else []), "--source-url", SOURCE_URL])
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" * 30))

    async def run() -> Any:
        async with world.serving():
            return await anyio.to_thread.run_sync(_exit_of_main)

    code = anyio.run(run)
    captured = capsys.readouterr()
    return code, click.unstyle(captured.out + captured.err)


def _names(folder: Path) -> set[str]:
    return {str(entry.relative_to(folder)) for entry in folder.rglob("*") if entry.is_file()}


def _kept_naming(out: str, album: Path, final: Path) -> list[str]:
    """The files the line saying the source is kept names, in the order printed."""
    said = f"remove_source_dir is on, but {album} is kept: it holds files {final} no longer has:"
    assert said in out, out
    named = []
    for line in out.split(said, 1)[1].splitlines()[1:]:
        if not line.startswith(" >> "):
            break
        named.append(line.removeprefix(" >> "))
    return named


# Changed: the source holds files the renamed folder no longer has, so it stays and they are named


@pytest.mark.parametrize("hardlinks", [True, False])
def test_up_keeps_a_source_holding_files_the_check_deleted(monkeypatch, capsys, dirs, hardlinks: bool) -> None:
    downloads, _torrents = dirs
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _with_extras(_album(downloads.parent / "seeding" / "Album"), *EXTRAS)

    code, out = _up(monkeypatch, capsys, dirs, album)

    final = downloads / RENAMED
    seen = (code, _names(album) >= set(EXTRAS), _names(final) & set(EXTRAS))
    assert seen == (0, True, set()), out
    # Only those two: the tracks, renamed in the renamed folder, are there under their new names.
    named = _kept_naming(out, album, final)
    assert named == list(EXTRAS), out
    assert REMOVED not in out


def test_up_keeps_the_source_when_the_extension_questions_take_their_default(monkeypatch, capsys, dirs) -> None:
    downloads, _torrents = dirs
    album = _with_extras(_album(downloads.parent / "seeding" / "Album"), *EXTRAS)

    code, out = _up(monkeypatch, capsys, dirs, album, yyy=False)

    final = downloads / RENAMED
    seen = (code, _names(album) >= set(EXTRAS), _names(final) & set(EXTRAS))
    assert seen == (0, True, set()), out
    named = _kept_naming(out, album, final)
    assert named == list(EXTRAS), out


def test_up_names_a_file_in_a_sub_folder_by_its_path(monkeypatch, capsys, dirs) -> None:
    downloads, _torrents = dirs
    album = _with_extras(_album(downloads.parent / "seeding" / "Album"), "Extras/video.mkv")

    code, out = _up(monkeypatch, capsys, dirs, album)

    named = _kept_naming(out, album, downloads / RENAMED)
    assert (code, album.is_dir(), named) == (0, True, ["Extras/video.mkv"]), out


def test_up_keeps_the_source_when_the_renamed_folder_went_to_a_seedbox(monkeypatch, capsys, dirs) -> None:
    downloads, _torrents = dirs
    album = _with_extras(_album(downloads.parent / "seeding" / "Album"), *EXTRAS)

    code, out = _up(monkeypatch, capsys, dirs, album, setup=_seedbox(remove_local_after_copy=True))

    seen = (code, (downloads / RENAMED).exists(), album.is_dir() and _names(album) >= set(EXTRAS))
    assert seen == (0, False, True), out
    assert _kept_naming(out, album, downloads / RENAMED) == list(EXTRAS), out


def test_a_file_that_cannot_be_read_keeps_the_source(tmp_path) -> None:
    from salmon.tagger.foldername import files_only_in  # new here: the file still imports on master

    source, final = tmp_path / "Album", tmp_path / RENAMED
    shutil.copytree(_with_extras(_album(source), "booklet.pdf"), final)
    (source / "link.pdf").symlink_to(tmp_path / "gone.pdf")

    assert files_only_in(str(source), str(final)) == ["link.pdf"]


def test_tag_keeps_a_source_holding_files_the_check_deleted(monkeypatch, dirs) -> None:
    downloads, _torrents = dirs
    album = _with_extras(_tag_album(downloads.parent / "seeding" / "Album"), *EXTRAS)

    result = _run_tag(monkeypatch, album, check_folder_structure=check_folder_structure)

    final = downloads / RENAMED
    seen = (result.exit_code, _names(album) >= set(EXTRAS), _names(final) & set(EXTRAS))
    assert seen == (0, True, set()), result.output
    named = _kept_naming(click.unstyle(result.output), album, final)
    assert named == list(EXTRAS), result.output


# As before: the source goes, or stays, as it did


def test_as_before_with_the_setting_off_nothing_is_said(monkeypatch, capsys, dirs) -> None:
    downloads, _torrents = dirs
    album = _with_extras(_album(downloads.parent / "seeding" / "Album"), *EXTRAS)

    code, out = _up(monkeypatch, capsys, dirs, album, remove_source_dir=False)

    assert (code, _names(album) >= set(EXTRAS)) == (0, True), out
    assert "remove_source_dir is on" not in out
    assert REMOVED not in out


@pytest.mark.parametrize("hardlinks", [True, False])
def test_as_before_a_source_with_nothing_deleted_goes(monkeypatch, capsys, dirs, hardlinks: bool) -> None:
    downloads, _torrents = dirs
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(downloads.parent / "seeding" / "Album")

    code, out = _up(monkeypatch, capsys, dirs, album)

    assert (code, album.exists()) == (0, False), out
    assert REMOVED in out


def test_as_before_hidden_files_and_an_eadir_folder_do_not_keep_the_source(monkeypatch, capsys, dirs) -> None:
    downloads, _torrents = dirs
    album = _with_extras(
        _album(downloads.parent / "seeding" / "Album"),
        ".DS_Store",
        ".AppleDouble/Icon",
        "@eaDir/cover.jpg@SynoEAStream",
    )

    code, out = _up(monkeypatch, capsys, dirs, album)

    assert (code, album.exists()) == (0, False), out
    assert REMOVED in out


def test_as_before_a_seedbox_and_nothing_deleted_lets_the_source_go(monkeypatch, capsys, dirs) -> None:
    downloads, _torrents = dirs
    album = _album(downloads.parent / "seeding" / "Album")

    code, out = _up(monkeypatch, capsys, dirs, album, setup=_seedbox(remove_local_after_copy=True))

    assert (code, album.exists(), (downloads / RENAMED).exists()) == (0, False, False), out
    assert REMOVED in out


def test_as_before_a_source_goes_when_nothing_compared_it_before_the_renamed_folder_went(monkeypatch, dirs) -> None:
    downloads, _torrents = dirs
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    album = _with_extras(_album(downloads.parent / "seeding" / "Album"), *EXTRAS)

    anyio.run(lambda: remove_source_folder(str(album), str(downloads / RENAMED)))

    assert not album.exists()


def test_as_before_tag_removes_a_source_with_nothing_deleted(monkeypatch, dirs) -> None:
    downloads, _torrents = dirs
    album = _tag_album(downloads.parent / "seeding" / "Album")

    result = _run_tag(monkeypatch, album, check_folder_structure=check_folder_structure)

    assert (result.exit_code, album.exists()) == (0, False), result.output
    assert REMOVED in result.output


@pytest.mark.parametrize("hardlinks", [True, False])
def test_as_before_tracks_rewritten_in_the_renamed_folder_let_the_source_go(
    monkeypatch, capsys, dirs, hardlinks: bool
) -> None:
    """A sanitize, or a re-run's tags, rewrites a track after the rename, under a new inode: it is still there."""
    downloads, _torrents = dirs
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(downloads.parent / "seeding" / "Album")

    async def rewriting_tracks(path: str, *args: Any, **kwargs: Any) -> None:
        for track in Path(path).rglob("*.flac"):
            rewritten = track.with_name(".rewritten")
            rewritten.write_bytes(track.read_bytes() + b"rewritten")
            rewritten.replace(track)
        await check_folder_structure(path, *args, **kwargs)

    code, out = _up(monkeypatch, capsys, dirs, album, check=rewriting_tracks)

    assert (code, album.exists()) == (0, False), out
    assert REMOVED in out
