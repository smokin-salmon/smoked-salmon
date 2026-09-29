"""library_dirs: an album inside one comes out of any salmon run byte-identical, and is never deleted."""

import hashlib
import os
import struct
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

import salmon.checks
import salmon.commands
import salmon.converter
import salmon.tagger
import salmon.tagger.foldername
import salmon.trackers
import salmon.uploader
from salmon import cfg
from salmon.config.validations import Directory
from salmon.errors import AbortAndDeleteFolder, UploadError
from salmon.uploader import staging
from salmon.uploader.spectrals import get_spectrals_path

RENAMED = "Artist - Album (2020) [WEB FLAC]"


def _write_flac(path: Path, **tags: str) -> None:
    """Write a FLAC file with no audio: a STREAMINFO block and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()


def _album(folder: Path) -> Path:
    """A release that every step of an upload would change: an alias tag, and an oversized embedded picture."""
    (folder / "CD1").mkdir(parents=True)
    # "year" is an alias standardize_tags rewrites to "date", in place.
    _write_flac(folder / "01 - one.flac", title="One", artist="Artist", year="2020")
    _write_flac(folder / "CD1" / "02 - two.flac", title="Two", artist="Artist", year="2020")
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = "image/jpeg"
    picture.data = b"x" * (1024 * 1024)
    tagged = FLAC(folder / "01 - one.flac")
    tagged.add_picture(picture)
    tagged.save()
    (folder / "cover.jpg").write_bytes(b"jpeg")
    # Old mtimes, so a rewrite shows even within the filesystem's timestamp resolution.
    for entry in folder.rglob("*"):
        os.utime(entry, ns=(1_000_000_000, 1_000_000_000))
    return folder


def _snapshot(folder: Path) -> dict[str, tuple[str, int, int]]:
    """Every entry under folder: a hash of its bytes (or "dir"), its mtime and its inode, by relative path."""
    return {
        str(entry.relative_to(folder)): (
            hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else "dir",
            entry.stat().st_mtime_ns,
            entry.stat().st_ino,
        )
        for entry in sorted(folder.rglob("*"))
    }


def _inodes(folder: Path) -> set[int]:
    return {entry.stat().st_ino for entry in folder.rglob("*") if entry.is_file()}


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A library and a download_directory beside it, configured."""
    library = tmp_path / "music"
    library.mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    return library, downloads


# is_library_path and protects


def test_a_library_root_and_everything_under_it_is_a_library_path(dirs) -> None:
    library, _downloads = dirs
    (library / "Artist" / "Album").mkdir(parents=True)

    assert cfg.directory.is_library_path(str(library))
    assert cfg.directory.is_library_path(str(library / "Artist" / "Album"))
    assert cfg.directory.is_library_path(str(library / "Artist" / "Album") + os.sep)


def test_a_sibling_sharing_the_library_prefix_is_not_a_library_path(dirs) -> None:
    library, _downloads = dirs
    sibling = library.parent / "music-old"
    (sibling / "Album").mkdir(parents=True)

    assert not cfg.directory.is_library_path(str(sibling))
    assert not cfg.directory.is_library_path(str(sibling / "Album"))
    assert not cfg.directory.protects(str(sibling / "Album"))


def test_a_symlink_into_the_library_is_a_library_path(dirs, tmp_path) -> None:
    library, _downloads = dirs
    (library / "Album").mkdir()
    link = tmp_path / "shortcut"
    link.symlink_to(library / "Album", target_is_directory=True)

    assert cfg.directory.is_library_path(str(link))
    assert cfg.directory.protects(str(link))


def test_a_library_behind_a_symlinked_entry_is_matched_on_its_real_path(monkeypatch, tmp_path) -> None:
    real = tmp_path / "disk" / "music"
    (real / "Album").mkdir(parents=True)
    (tmp_path / "music").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(tmp_path / "music")])

    assert cfg.directory.is_library_path(str(real / "Album"))


def test_the_filesystem_root_as_a_library_holds_everything(monkeypatch) -> None:
    monkeypatch.setattr(cfg.directory, "library_dirs", [os.path.abspath(os.sep)])

    assert cfg.directory.is_library_path(os.path.join(os.sep, "data", "album"))


def test_a_folder_holding_a_library_is_protected_but_not_a_library_path(dirs) -> None:
    library, _downloads = dirs

    assert not cfg.directory.is_library_path(str(library.parent))
    assert cfg.directory.library_inside(str(library.parent)) == str(library)
    assert cfg.directory.protects(str(library.parent))


def test_without_library_dirs_nothing_is_protected(monkeypatch, tmp_path) -> None:
    assert Directory(dottorrents_dir=str(tmp_path), download_directory=str(tmp_path)).library_dirs == []
    monkeypatch.setattr(cfg.directory, "library_dirs", [])

    assert not cfg.directory.is_library_path(str(tmp_path))
    assert not cfg.directory.protects(os.path.abspath(os.sep))


# Config validation


def test_a_library_entry_that_is_not_a_directory_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="library_dirs entry is not a valid directory"):
        Directory(
            dottorrents_dir=str(tmp_path), download_directory=str(tmp_path), library_dirs=[str(tmp_path / "missing")]
        )


@pytest.mark.parametrize("field", ["download_directory", "dottorrents_dir", "tmp_dir"])
def test_a_library_entry_holding_a_folder_salmon_writes_in_is_refused(tmp_path, field: str) -> None:
    library = tmp_path / "data"
    inside = library / "torrents"
    inside.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    folders: dict[str, str | None] = {
        "download_directory": str(tmp_path / "elsewhere"),
        "dottorrents_dir": str(tmp_path / "elsewhere"),
        "tmp_dir": None,
        field: str(inside),
    }

    with pytest.raises(ValueError, match=f"must not contain {field}"):
        Directory(
            download_directory=str(folders["download_directory"]),
            dottorrents_dir=str(folders["dottorrents_dir"]),
            tmp_dir=folders["tmp_dir"],
            library_dirs=[str(library)],
        )


def test_a_library_entry_inside_tmp_dir_is_refused(tmp_path) -> None:
    library = tmp_path / "tmp" / "music"
    library.mkdir(parents=True)

    with pytest.raises(ValueError, match="must not be inside tmp_dir"):
        Directory(
            dottorrents_dir=str(tmp_path),
            download_directory=str(tmp_path),
            tmp_dir=str(tmp_path / "tmp"),
            library_dirs=[str(library)],
        )


def test_a_library_beside_the_folders_salmon_writes_in_is_accepted(tmp_path) -> None:
    library = tmp_path / "media" / "music"
    library.mkdir(parents=True)
    downloads = tmp_path / "torrents"
    downloads.mkdir()

    directory = Directory(
        dottorrents_dir=str(downloads), download_directory=str(downloads), library_dirs=[str(library)]
    )

    assert directory.library_dirs == [str(library)]


# staged_source


def test_a_library_album_is_staged_as_a_real_copy_whose_rename_goes_to_download_directory(dirs, capsys) -> None:
    library, downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(album)

    with staging.staged_source(str(album), scratch=False) as (staged, rename_into):
        assert rename_into is None
        assert os.path.dirname(os.path.dirname(staged)) == str(downloads / staging.STAGING_DIR)
        assert _inodes(Path(staged)).isdisjoint(_inodes(album))
        Path(staged, "cover.jpg").write_bytes(b"changed")

    assert _snapshot(album) == before
    assert os.listdir(downloads / staging.STAGING_DIR) == []
    assert "library_dirs" in capsys.readouterr().out


def test_a_folder_holding_a_library_is_refused(dirs) -> None:
    library, _downloads = dirs

    with (
        pytest.raises(UploadError, match="holds the library folder"),
        staging.staged_source(str(library.parent), scratch=False),
    ):
        pytest.fail("the upload ran on a folder holding a library")


# salmon up


class FakeSite:
    site_code = "RED"
    site_string = "RED"
    base_url = "https://tracker.test"


def _returning(result: Any = None):
    def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _returning_async(result: Any = None):
    async def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _retag(path: str, *_args: Any) -> None:
    """Stands in for tag_files: rewrites a tag in every FLAC of the folder it is given."""
    for root, _dirs, files in os.walk(path):
        for name in files:
            if name.endswith(".flac"):
                tagged = FLAC(os.path.join(root, name))
                tagged["album"] = "Album"
                tagged.save()


def _rename_files(path: str, *_args: Any) -> None:
    """Stands in for rename_files: renames the tracks of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            os.rename(os.path.join(path, name), os.path.join(path, name.replace(" - ", ". ")))


def _run_up(monkeypatch, album: Path, **fakes: Any) -> tuple[Any, list[str], list[tuple[str, str]]]:
    """Run `salmon up ALBUM -g 5` with the real staging, tag standardizing, picture strip and folder rename.

    The seams that need audio tools, a network or a reviewer are stubbed, `fakes` replace more of them.
    Returns the result, the folder of every upload, and the (source, output) of every transcode.
    """
    uploads: list[str] = []
    transcodes: list[tuple[str, str]] = []

    async def upload_and_report(_site: Any, path: str, *_args: Any, **_kwargs: Any) -> tuple:
        uploads.append(path)
        return 21, 5, "/t.torrent", b"", "https://tracker.test/t"

    async def transcode(path: str, bitrate: str, *_args: Any, output_dir: str | None = None, **_kw: Any) -> str:
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
        "confirm_group_upload": _returning_async({}),
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
        "print_torrents": _returning_async(),
        "UploadManager": FakeUploadManager,
        "transcode_folder": transcode,
        "upload_and_report": upload_and_report,
        **fakes,
    }.items():
        monkeypatch.setattr(salmon.uploader, name, fake)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    # -yyy sets yes_all: patched, so it is put back after the test.
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(cfg.upload.requests, "check_requests", False)
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(cfg.image, "auto_compress_cover", False)
    monkeypatch.setattr(salmon.trackers, "get_class", lambda _tracker: FakeSite)

    async def run():
        return await CliRunner().invoke(
            salmon.uploader.up,
            [str(album), "-t", "RED", "-g", "5", "-s", "WEB", "-n", "-yyy", "--skip-integrity-check", "--skip-up"],
            input="y\n",
        )

    return anyio.run(run), uploads, transcodes


@pytest.mark.parametrize("remove_source_dir", [False, True])
@pytest.mark.parametrize("hardlinks", [True, False])
def test_up_leaves_a_library_album_byte_identical_and_uploads_a_copy(
    monkeypatch, dirs, hardlinks: bool, remove_source_dir: bool
) -> None:
    library, downloads = dirs
    album = _album(library / "Artist" / "Album")
    before = _snapshot(library)
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", remove_source_dir)

    result, uploads, transcodes = _run_up(monkeypatch, album)

    assert result.exit_code == 0, result.output
    assert _snapshot(library) == before
    # The upload and both transcodes worked on the renamed copy, which stays in download_directory to seed.
    copy = downloads / RENAMED
    assert uploads == [str(copy), f"{copy} [320]", f"{copy} [V0]"]
    assert [source for source, _output in transcodes] == [str(copy)] * 2
    assert set(os.listdir(downloads)) == {staging.STAGING_DIR, RENAMED, f"{RENAMED} [320]", f"{RENAMED} [V0]"}
    assert os.listdir(downloads / staging.STAGING_DIR) == []
    # Every change the run makes landed in the copy: the retag, the alias rewrite, the picture strip, the renames.
    one = FLAC(copy / "01. one.flac")
    assert (one["album"], one["date"], one.pictures) == (["Album"], ["2020"], [])
    assert "year" not in one
    assert FLAC(copy / "CD1" / "02 - two.flac")["album"] == ["Album"]
    assert _inodes(copy).isdisjoint(_inodes(library))
    assert all(os.stat(file).st_nlink == 1 for file in library.rglob("*") if file.is_file())


@pytest.mark.parametrize("when", ["before the rename", "after the rename"])
def test_abort_and_delete_keeps_the_library_album_and_deletes_the_copy(monkeypatch, dirs, when: str) -> None:
    library, downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(library)
    deleted: list[str] = []

    async def delete_before(*_args: Any, **_kwargs: Any) -> None:
        raise AbortAndDeleteFolder

    def delete_after(path: str) -> None:
        deleted.append(path)
        raise AbortAndDeleteFolder

    fakes = (
        {"check_spectrals": delete_before} if when == "before the rename" else {"check_embedded_pictures": delete_after}
    )
    monkeypatch.setattr(cfg.upload, "windows_use_recycle_bin", False)

    result, uploads, _transcodes = _run_up(monkeypatch, album, **fakes)

    assert result.exit_code == 0, result.output
    assert uploads == []
    assert _snapshot(library) == before
    assert f"The library album {album} is kept" in result.output
    assert "Deleted folder" in result.output
    # What was deleted is the copy: nothing of this run is left in download_directory.
    assert deleted == ([] if when == "before the rename" else [str(downloads / RENAMED)])
    assert os.listdir(downloads) == [staging.STAGING_DIR]
    assert os.listdir(downloads / staging.STAGING_DIR) == []


def test_abort_and_delete_never_deletes_a_folder_in_a_library(monkeypatch, dirs) -> None:
    # Defence in depth: the folder the upload works on is a copy, but if it ever were a library album, it is kept.
    library, _downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(library)
    monkeypatch.setattr(salmon.uploader, "staged_source", _not_staged)

    async def delete(*_args: Any, **_kwargs: Any) -> None:
        raise AbortAndDeleteFolder

    result, _uploads, _transcodes = _run_up(monkeypatch, album, standardize_tags=_returning(), check_spectrals=delete)

    assert result.exit_code == 0, result.output
    assert "Not deleting" in result.output
    assert _snapshot(library) == before


class _not_staged:
    """A staged_source that hands the upload the folder itself."""

    def __init__(self, path: str, scratch: bool) -> None:
        self.path = path

    def __enter__(self) -> tuple[str, None]:
        return self.path, None

    def __exit__(self, *_exc: object) -> None:
        return None


# rename_folder


def _metadata() -> dict[str, Any]:
    return {
        "scene": False,
        "artists": [("Artist", "main")],
        "title": "Album",
        "year": 2020,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
    }


@pytest.mark.parametrize("hardlinks", [True, False])
def test_rename_folder_never_hardlinks_or_removes_a_library_album(monkeypatch, dirs, hardlinks: bool) -> None:
    library, downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(library)
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))

    new_path = salmon.tagger.foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    assert new_path == str(downloads / RENAMED)
    assert _snapshot(library) == before
    assert _inodes(Path(new_path)).isdisjoint(_inodes(library))


def test_rename_folder_never_replaces_a_library_with_the_renamed_folder(monkeypatch, tmp_path) -> None:
    # download_directory may hold a library: a renamed folder named like it must not replace it.
    downloads = tmp_path / "downloads"
    library = downloads / RENAMED
    _album(library / "Album")
    before = _snapshot(library)
    source = _album(tmp_path / "seeding" / "Album")
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))

    with pytest.raises(UploadError, match="library_dirs"):
        salmon.tagger.foldername.rename_folder(str(source), _metadata(), auto_rename=True, check=False)

    assert _snapshot(library) == before


# Other commands that change a folder


def test_tag_works_on_a_copy_renamed_into_download_directory(monkeypatch, dirs) -> None:
    library, downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(library)
    metadata = {**_metadata(), "cover": None}
    for name, fake in {
        "gather_tags": _returning({}),
        "gather_audio_info": _returning({}),
        "construct_rls_data": _returning({}),
        "get_metadata": _returning_async((metadata, None)),
        "review_metadata_with_ai": _returning_async(metadata),
        "tag_files": _retag,
        "download_cover_if_nonexistent": _returning_async(),
        "check_tags": _returning_async({}),
        "rename_files": _rename_files,
        "check_folder_structure": _returning_async(),
    }.items():
        monkeypatch.setattr(salmon.tagger, name, fake)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    monkeypatch.setattr(cfg.upload, "yes_all", True)

    async def run():
        return await CliRunner().invoke(salmon.tagger.tag, [str(album), "-s", "WEB", "-n"])

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert _snapshot(library) == before
    assert set(os.listdir(downloads)) == {staging.STAGING_DIR, RENAMED}
    one = FLAC(downloads / RENAMED / "01. one.flac")
    assert (one["album"], one["date"]) == (["Album"], ["2020"])


def test_compress_refuses_a_library_album(monkeypatch, dirs) -> None:
    library, _downloads = dirs
    album = _album(library / "Album")
    recompressed: list[str] = []

    async def recompress(path: str) -> None:
        recompressed.append(path)

    monkeypatch.setattr(salmon.commands, "recompress", recompress)

    async def run():
        return await CliRunner().invoke(salmon.commands.compress, [str(album)])

    result = anyio.run(run)

    assert result.exit_code != 0
    assert "library_dirs" in result.output
    assert recompressed == []


@pytest.mark.parametrize("target", ["folder", "file"])
def test_check_integrity_does_not_offer_to_sanitize_a_library_album(monkeypatch, dirs, target: str) -> None:
    library, _downloads = dirs
    album = _album(library / "Album")
    path = album if target == "folder" else album / "01 - one.flac"
    sanitized: list[str] = []

    async def sanitize(path: str) -> None:
        sanitized.append(path)

    # salmon.checks.integrity is the command; the module is only reachable through sys.modules.
    integrity = sys.modules["salmon.checks.integrity"]
    monkeypatch.setattr(integrity, "check_integrity", _returning_async(integrity.IntegrityResult(passed=False)))
    monkeypatch.setattr(integrity, "sanitize_and_verify", sanitize)

    async def run():
        return await CliRunner().invoke(salmon.checks.check, ["integrity", str(path)], input="y\n")

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert "Not offering to sanitize" in result.output
    assert sanitized == []


@pytest.mark.parametrize("command", ["transcode", "downconv"])
def test_conversions_of_a_library_album_go_to_download_directory(monkeypatch, dirs, command: str) -> None:
    library, downloads = dirs
    album = _album(library / "Album")
    output_dirs: list[str | None] = []

    async def convert(_path: str, *_args: Any, output_dir: str | None = None, **_kwargs: Any) -> None:
        output_dirs.append(output_dir)

    monkeypatch.setattr(salmon.converter, "transcode_folder", convert)
    monkeypatch.setattr(salmon.converter, "convert_folder", convert)
    args = [str(album), "-b", "V0"] if command == "transcode" else [str(album)]

    async def run():
        return await CliRunner().invoke(getattr(salmon.converter, command), args)

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert output_dirs == [str(downloads)]


def test_conversions_outside_a_library_still_go_beside_the_source(monkeypatch, dirs, tmp_path) -> None:
    album = _album(tmp_path / "seeding" / "Album")
    output_dirs: list[str | None] = []

    async def convert(_path: str, *_args: Any, output_dir: str | None = None, **_kwargs: Any) -> None:
        output_dirs.append(output_dir)

    monkeypatch.setattr(salmon.converter, "convert_folder", convert)

    async def run():
        return await CliRunner().invoke(salmon.converter.downconv, [str(album)])

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert output_dirs == [None]


def test_spectrals_of_a_library_album_are_made_outside_it(dirs, tmp_path) -> None:
    library, downloads = dirs

    assert get_spectrals_path(str(library / "Album")) == str(downloads / "spectrals_Album")
    assert get_spectrals_path(str(tmp_path / "seeding" / "Album")) == str(tmp_path / "seeding" / "Album" / "Spectrals")
