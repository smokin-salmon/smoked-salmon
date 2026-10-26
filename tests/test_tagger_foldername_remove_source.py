"""remove_source_dir removes the source folder when the run ends, never at the rename, and says what became of it.

Why it is kept is said as in #680."""

from pathlib import Path
from typing import Any

import anyio
import pytest

from salmon import cfg, dryrun, interaction
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


def _remove(source: Path, final: str | None, original: Path | None = None, finished: bool = True) -> None:
    anyio.run(
        lambda: foldername.remove_source_folder(
            str(source), final, str(original) if original else None, finished=finished
        )
    )


def _answering(monkeypatch, answer: bool) -> list[str]:
    asked: list[str] = []

    async def confirm(text: str, **_kwargs: Any) -> bool:
        asked.append(text)
        return answer

    monkeypatch.setattr(interaction, "confirm", confirm)
    return asked


def test_the_rename_keeps_the_source_folder(dirs, capsys) -> None:
    # A run aborted after the rename must leave the album where the user can run salmon on it again.
    _downloads, seeding = dirs
    album = _album(seeding / "Album")

    new_path = _rename(album)

    assert (album / "01 - one.flac").read_bytes() == b"one"
    assert (Path(new_path) / "01 - one.flac").read_bytes() == b"one"
    assert "Removed the source folder" not in capsys.readouterr().out


def test_the_source_folder_is_removed_at_the_end_when_the_user_says_yes(monkeypatch, dirs, capsys) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    new_path = _rename(album)
    asked = _answering(monkeypatch, True)

    _remove(album, new_path)

    assert not album.exists()
    assert (Path(new_path) / "01 - one.flac").read_bytes() == b"one"
    assert len(asked) == 1 and str(album) in asked[0] and new_path in asked[0]
    assert f"Removed the source folder {album} (remove_source_dir)." in capsys.readouterr().out


def test_no_keeps_the_source_folder(monkeypatch, dirs, capsys) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    new_path = _rename(album)
    _answering(monkeypatch, False)

    _remove(album, new_path)

    assert album.exists()
    assert f"Kept the source folder {album}." in capsys.readouterr().out


def test_a_run_assuming_defaults_removes_it_without_asking(monkeypatch, dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    new_path = _rename(album)
    asked = _answering(monkeypatch, False)

    with interaction.assuming_defaults():
        _remove(album, new_path)

    assert not album.exists()
    assert asked == []


def test_a_run_that_did_not_finish_keeps_the_source_folder(monkeypatch, dirs, capsys) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    new_path = _rename(album)
    asked = _answering(monkeypatch, True)

    _remove(album, new_path, finished=False)

    assert album.exists()
    assert asked == []
    assert f"remove_source_dir is on, but {album} is kept: the run did not finish." in capsys.readouterr().out


def test_a_run_stopped_before_the_rename_says_nothing(monkeypatch, dirs, capsys) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    asked = _answering(monkeypatch, True)

    _remove(album, None)

    assert album.exists()
    assert asked == []
    assert "remove_source_dir" not in capsys.readouterr().out


def test_the_same_folder_is_never_removed(monkeypatch, dirs) -> None:
    downloads, _seeding = dirs
    album = _album(downloads / NEW_NAME)
    asked = _answering(monkeypatch, True)

    _remove(album, str(album))

    assert album.exists()
    assert asked == []


def test_a_library_album_kept_is_said(monkeypatch, dirs, tmp_path, capsys) -> None:
    library = tmp_path / "music"
    original = _album(library / "Album")
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    staged = _album(tmp_path / "staged-copy")
    new_path = _rename(staged, original)

    _remove(staged, new_path, original)

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
        new_path = _rename(staged, original)
        _remove(staged, new_path, original)

    out = capsys.readouterr().out
    assert f"remove_source_dir is on, but {original} is kept: a dry run works on a copy and changes nothing." in out
    assert (original / "01 - one.flac").exists()


def test_a_skip_flac_copy_keeps_its_source_and_says_why(dirs, capsys) -> None:
    _downloads, seeding = dirs
    original = _album(seeding / "Album")
    staged = _album(seeding / "staged-copy")
    new_path = _rename(staged, original)

    _remove(staged, new_path, original)

    out = capsys.readouterr().out
    assert f"remove_source_dir is on, but {original} is kept: --skip-flac-upload works on a copy." in out
    assert (original / "01 - one.flac").exists()


def test_the_setting_off_says_nothing_new(monkeypatch, dirs, capsys) -> None:
    _downloads, seeding = dirs
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", False)
    album = _album(seeding / "Album")
    new_path = _rename(album)
    asked = _answering(monkeypatch, True)

    _remove(album, new_path)

    out = capsys.readouterr().out
    assert "remove_source_dir" not in out
    assert "Removed the source folder" not in out
    assert album.exists()
    assert asked == []
