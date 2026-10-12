"""remove_source_dir says what became of the source folder: removed, or kept and why (#680)."""

from pathlib import Path
from typing import Any

import anyio
import pytest

from salmon import cfg, dryrun
from salmon.tagger import foldername

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
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a place for albums outside it; remove_source_dir on, no library."""
    downloads, seeding = tmp_path / "downloads", tmp_path / "seeding"
    downloads.mkdir()
    seeding.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    monkeypatch.setattr(foldername, "generate_folder_name", lambda _metadata: NEW_NAME)
    return downloads, seeding


def _album(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "01 - one.flac").write_bytes(b"one")
    return folder


def _rename(path: Path, original: Path | None = None) -> str:
    return anyio.run(
        lambda: foldername.rename_folder(
            str(path), METADATA, auto_rename=True, check=False, original=str(original) if original else None
        )
    )


def test_a_removed_source_folder_is_said(dirs, capsys) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")

    new_path = _rename(album)

    assert not album.exists()
    assert (Path(new_path) / "01 - one.flac").read_bytes() == b"one"
    assert f"Removed the source folder {album} (remove_source_dir)." in capsys.readouterr().out


def test_a_library_album_kept_is_said(monkeypatch, dirs, tmp_path, capsys) -> None:
    library = tmp_path / "music"
    original = _album(library / "Album")
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    staged = _album(tmp_path / "staged-copy")

    new_path = _rename(staged, original)

    out = capsys.readouterr().out
    assert f"remove_source_dir is on, but {original} is kept:" in out
    assert "library_dirs" in out
    assert (original / "01 - one.flac").exists()
    assert (Path(new_path) / "01 - one.flac").exists()
    assert "Removed the source folder" not in out


def test_a_dry_run_copy_keeps_its_source_and_says_why(dirs, capsys) -> None:
    _downloads, seeding = dirs
    original = _album(seeding / "Album")
    staged = _album(seeding / "staged-copy")

    with dryrun.mode():
        _rename(staged, original)

    out = capsys.readouterr().out
    assert f"remove_source_dir is on, but {original} is kept: a dry run works on a copy and changes nothing." in out
    assert (original / "01 - one.flac").exists()


def test_a_skip_flac_copy_keeps_its_source_and_says_why(dirs, capsys) -> None:
    _downloads, seeding = dirs
    original = _album(seeding / "Album")
    staged = _album(seeding / "staged-copy")

    _rename(staged, original)

    out = capsys.readouterr().out
    assert f"remove_source_dir is on, but {original} is kept: --skip-flac-upload works on a copy." in out
    assert (original / "01 - one.flac").exists()


def test_the_setting_off_says_nothing_new(monkeypatch, dirs, capsys) -> None:
    _downloads, seeding = dirs
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", False)
    original = _album(seeding / "Album")
    staged = _album(seeding / "staged-copy")

    _rename(staged, original)

    out = capsys.readouterr().out
    assert "remove_source_dir" not in out
    assert "Removed the source folder" not in out
    assert staged.exists()
    assert original.exists()
