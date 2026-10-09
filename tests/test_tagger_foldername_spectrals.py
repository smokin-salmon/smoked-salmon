"""A folder rename never leaves the spectrals of the old name behind (#569, #573)."""

import hashlib
from pathlib import Path
from typing import Any

import anyio
import pytest

from salmon import cfg
from salmon.tagger import foldername
from salmon.uploader.spectrals import get_spectrals_path

NEW_NAME = "Artist - Album (2020) [WEB FLAC]"
SPECTRAL_FILES = {"01 Full.png": b"full", "01 Zoom.png": b"zoom"}


def _metadata(scene: bool = False) -> dict[str, Any]:
    return {
        "scene": scene,
        "artists": [("Artist", "main")],
        "title": "Album",
        "year": 2020,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
    }


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a place for albums that is not in it; no tmp_dir, no library, nothing removed."""
    downloads, seeding = tmp_path / "downloads", tmp_path / "seeding"
    downloads.mkdir()
    seeding.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", False)
    monkeypatch.setattr(foldername, "generate_folder_name", lambda _metadata: NEW_NAME)
    return downloads, seeding


def _album(folder: Path) -> Path:
    folder.mkdir()
    (folder / "01 - one.flac").write_bytes(b"one")
    (folder / "02 - two.flac").write_bytes(b"two")
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


def _write_spectrals(spectrals: Path) -> None:
    spectrals.mkdir(parents=True)
    for name, data in SPECTRAL_FILES.items():
        (spectrals / name).write_bytes(data)


def _listing(folder: Path) -> dict[str, str]:
    """Every entry under folder, by relative path: a hash of its bytes, or "dir"."""
    return {
        str(entry.relative_to(folder)): hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else "dir"
        for entry in sorted(folder.rglob("*"))
    }


def _files(folder: Path) -> dict[str, bytes]:
    return {entry.name: entry.read_bytes() for entry in folder.iterdir()}


@pytest.mark.parametrize("hardlinks", [True, False])
def test_the_tmp_dir_spectrals_move_to_the_new_name(monkeypatch, dirs, tmp_path, hardlinks: bool) -> None:
    _downloads, seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(seeding / "Old Name")
    _write_spectrals(tmp_dir / "spectrals_Old Name")

    new_path = anyio.run(lambda: foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False))

    assert Path(new_path).name == NEW_NAME
    assert sorted(entry.name for entry in tmp_dir.iterdir()) == [f"spectrals_{NEW_NAME}"]
    assert _files(tmp_dir / f"spectrals_{NEW_NAME}") == SPECTRAL_FILES
    assert Path(get_spectrals_path(new_path)) == tmp_dir / f"spectrals_{NEW_NAME}"


def test_a_stale_tmp_dir_spectrals_folder_of_the_new_name_is_replaced(monkeypatch, dirs, tmp_path) -> None:
    _downloads, seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    album = _album(seeding / "Old Name")
    _write_spectrals(tmp_dir / "spectrals_Old Name")
    (tmp_dir / f"spectrals_{NEW_NAME}").mkdir()
    (tmp_dir / f"spectrals_{NEW_NAME}" / "stale.png").write_bytes(b"stale")

    anyio.run(lambda: foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False))

    assert sorted(entry.name for entry in tmp_dir.iterdir()) == [f"spectrals_{NEW_NAME}"]
    assert _files(tmp_dir / f"spectrals_{NEW_NAME}") == SPECTRAL_FILES


@pytest.mark.parametrize("hardlinks", [True, False])
@pytest.mark.parametrize("scene", [False, True])
def test_the_source_spectrals_move_into_the_renamed_copy(monkeypatch, dirs, hardlinks: bool, scene: bool) -> None:
    downloads, seeding = dirs
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(seeding / "Old Name")
    before = _listing(album)
    _write_spectrals(Path(get_spectrals_path(str(album))))

    new_path = Path(
        anyio.run(lambda: foldername.rename_folder(str(album), _metadata(scene), auto_rename=True, check=False))
    )

    assert new_path.parent == downloads
    assert new_path.name == ("Old Name" if scene else NEW_NAME)
    assert not (album / "Spectrals").exists()
    assert _listing(album) == before
    assert _files(Path(get_spectrals_path(str(new_path)))) == SPECTRAL_FILES
    assert {key: value for key, value in _listing(new_path).items() if not key.startswith("Spectrals")} == before


@pytest.mark.parametrize("hardlinks", [True, False])
def test_a_user_folder_named_spectrals_below_the_top_level_is_still_copied(monkeypatch, dirs, hardlinks: bool) -> None:
    _downloads, seeding = dirs
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(seeding / "Old Name")
    (album / "Extras" / "Spectrals").mkdir(parents=True)
    (album / "Extras" / "Spectrals" / "mine.png").write_bytes(b"mine")
    before = _listing(album)
    _write_spectrals(Path(get_spectrals_path(str(album))))

    new_path = Path(anyio.run(lambda: foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)))

    assert _listing(album) == before
    assert (new_path / "Extras" / "Spectrals" / "mine.png").read_bytes() == b"mine"
    assert _files(new_path / "Spectrals") == SPECTRAL_FILES


def test_an_album_without_spectrals_is_copied_as_before(dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Old Name")
    before = _listing(album)

    new_path = Path(anyio.run(lambda: foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)))

    assert _listing(album) == before
    assert _listing(new_path) == before


@pytest.mark.parametrize("remove_source_dir", [False, True])
def test_a_plain_file_named_spectrals_is_copied_like_any_other(monkeypatch, dirs, remove_source_dir: bool) -> None:
    _downloads, seeding = dirs
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", remove_source_dir)
    album = _album(seeding / "Old Name")
    (album / "Spectrals").write_bytes(b"not a folder")

    new_path = Path(anyio.run(lambda: foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)))

    assert (new_path / "Spectrals").read_bytes() == b"not a folder"


def test_the_spectrals_of_a_library_album_are_never_inside_it(monkeypatch, dirs) -> None:
    downloads, _seeding = dirs
    library = downloads.parent / "library"
    library.mkdir()
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    album = _album(library / "Old Name")
    _write_spectrals(Path(get_spectrals_path(str(album))))
    before = _listing(library)

    assert not Path(get_spectrals_path(str(album))).is_relative_to(album)

    anyio.run(lambda: foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False))

    assert _listing(library) == before
