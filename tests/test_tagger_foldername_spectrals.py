"""A folder rename moves the spectrals salmon made to the new name, never leaving them behind (#569, #573). A spectrals
folder salmon did not make is never replaced, moved or deleted: an album's own is copied like any other folder."""

import hashlib
import os
from pathlib import Path
from typing import Any

import anyio
import pytest

from salmon import cfg, dryrun
from salmon.errors import UploadError
from salmon.tagger import foldername
from salmon.uploader import spectrals
from salmon.uploader.spectrals import create_specs_folder, get_spectrals_path, made_by_salmon

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
    _fill(spectrals)


def _fill(spectrals: Path) -> None:
    for name, data in SPECTRAL_FILES.items():
        (spectrals / name).write_bytes(data)


def _specs(album: Path | str) -> Path:
    """Where salmon makes an album's spectrals."""
    return Path(get_spectrals_path(str(album)))


def _rename(album: Path, scene: bool = False) -> Path:
    return Path(
        anyio.run(lambda: foldername.rename_folder(str(album), _metadata(scene), auto_rename=True, check=False))
    )


def _tmp_dir(monkeypatch, tmp_path: Path) -> Path:
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    return tmp_dir


def _no_upload(monkeypatch) -> None:
    async def no_upload(*_args: Any, **_kwargs: Any) -> dict:
        return {}

    monkeypatch.setattr(spectrals, "upload_spectrals", no_upload)


def _listing(folder: Path) -> dict[str, str]:
    """Every entry under folder, by relative path: a hash of its bytes, or "dir"."""
    return {
        str(entry.relative_to(folder)): hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else "dir"
        for entry in sorted(folder.rglob("*"))
    }


def _files(folder: Path) -> dict[str, bytes]:
    return {entry.name: entry.read_bytes() for entry in folder.iterdir()}


@pytest.mark.parametrize("hardlinks", [True, False])
@pytest.mark.parametrize("scene", [False, True])
def test_the_tmp_dir_spectrals_move_to_the_new_name(monkeypatch, dirs, tmp_path, hardlinks: bool, scene: bool) -> None:
    downloads, seeding = dirs
    tmp_dir = _tmp_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(seeding / "Old Name")
    _write_spectrals(_specs(album))

    new_path = _rename(album, scene)

    assert new_path == downloads / ("Old Name" if scene else NEW_NAME)
    assert _specs(new_path).parent == tmp_dir
    assert _specs(new_path).name.startswith(f"spectrals_{new_path.name} ")
    assert sorted(entry.name for entry in tmp_dir.iterdir()) == [_specs(new_path).name]
    assert _files(_specs(new_path)) == SPECTRAL_FILES


def test_a_stale_tmp_dir_spectrals_folder_of_the_new_name_is_replaced(monkeypatch, dirs, tmp_path) -> None:
    downloads, seeding = dirs
    tmp_dir = _tmp_dir(monkeypatch, tmp_path)
    album = _album(seeding / "Old Name")
    _write_spectrals(_specs(album))
    # Salmon's, from an earlier run of the album at the new name.
    stale = _specs(downloads / NEW_NAME)
    stale.mkdir()
    (stale / "01 Full.png").write_bytes(b"stale")
    (stale / "02 Full.png").write_bytes(b"stale")

    new_path = _rename(album)

    assert _specs(new_path) == stale
    assert sorted(entry.name for entry in tmp_dir.iterdir()) == [stale.name]
    assert _files(stale) == SPECTRAL_FILES


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


# A spectrals folder salmon did not make


@pytest.mark.parametrize("own", [{"notes.txt": b"mine"}, {"01 Full.png": b"theirs", "notes.txt": b"mine"}, {}])
def test_an_albums_own_spectrals_folder_stays_in_it_and_goes_into_the_renamed_copy(dirs, own: dict) -> None:
    downloads, seeding = dirs
    album = _album(seeding / "Old Name")
    (album / "Spectrals").mkdir()
    for name, data in own.items():
        (album / "Spectrals" / name).write_bytes(data)
    before = _listing(album)

    made = Path(create_specs_folder(str(album)))
    _fill(made)
    new_path = _rename(album)

    assert made.parent == downloads
    assert _listing(album) == before
    assert _files(new_path / "Spectrals") == own
    assert _specs(new_path).parent == downloads
    assert _files(_specs(new_path)) == SPECTRAL_FILES
    assert not made.exists()


def test_spectrals_an_earlier_run_left_in_the_album_are_still_replaced_and_deleted(monkeypatch, dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    left = album / "Spectrals"
    left.mkdir()
    # An earlier run's spectrals, averaged spectra and an oxipng copy it was killed in, beside a file browser's litter.
    for name in ("01 Full.png", "01 Zoom.png", "01 Spectrum.png", "02 Full.png.tmp.png", ".DS_Store", "Thumbs.db"):
        (left / name).write_bytes(b"old")
    _no_upload(monkeypatch)

    made = create_specs_folder(str(album))
    assert Path(made) == left
    assert list(left.iterdir()) == []
    _fill(left)
    anyio.run(spectrals.handle_spectrals_upload_and_deletion, made, {1: "01 - one.flac"})

    assert not left.exists()


def test_an_empty_spectrals_folder_salmon_did_not_make_is_the_albums_own(dirs) -> None:
    downloads, seeding = dirs
    album = _album(seeding / "Album")
    (album / "Spectrals").mkdir()

    made = create_specs_folder(str(album))

    assert (album / "Spectrals").is_dir()
    assert Path(made).parent == downloads


def test_a_symlinked_spectrals_folder_is_never_salmons(dirs, tmp_path) -> None:
    downloads, seeding = dirs
    album = _album(seeding / "Album")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "01 Full.png").write_bytes(b"theirs")
    (album / "Spectrals").symlink_to(elsewhere)

    made = create_specs_folder(str(album))

    assert not made_by_salmon(str(album / "Spectrals"))
    assert (elsewhere / "01 Full.png").read_bytes() == b"theirs"
    assert (album / "Spectrals").is_symlink()
    assert Path(made).parent == downloads


def test_a_file_named_spectrals_in_the_album_gets_salmons_folder_beside_it(dirs) -> None:
    downloads, seeding = dirs
    album = _album(seeding / "Album")
    (album / "Spectrals").write_bytes(b"not a folder")

    made = create_specs_folder(str(album))

    assert (album / "Spectrals").read_bytes() == b"not a folder"
    assert Path(made).parent == downloads


@pytest.mark.parametrize("theirs", ["a file", "a folder named like a spectral", "a hidden folder"])
def test_making_spectrals_over_a_folder_that_is_not_salmons_is_refused(monkeypatch, dirs, tmp_path, theirs) -> None:
    _downloads, seeding = dirs
    _tmp_dir(monkeypatch, tmp_path)
    album = _album(seeding / "Album")
    specs = _specs(album)
    specs.mkdir()
    (specs / "01 Zoom.png").write_bytes(b"png")
    holder = {"a file": specs, "a folder named like a spectral": specs / "01 Full.png", "a hidden folder": specs / ".x"}
    holder[theirs].mkdir(exist_ok=True)
    (holder[theirs] / "theirs.txt").write_bytes(b"theirs")

    with pytest.raises(UploadError, match="salmon did not make"):
        create_specs_folder(str(album))

    assert (holder[theirs] / "theirs.txt").read_bytes() == b"theirs"


def test_deleting_spectrals_leaves_a_folder_holding_files_salmon_did_not_make(monkeypatch, dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    made = Path(create_specs_folder(str(album)))
    _fill(made)
    (made / "notes.txt").write_bytes(b"mine")
    _no_upload(monkeypatch)

    anyio.run(spectrals.handle_spectrals_upload_and_deletion, str(made), {1: "01 - one.flac"})

    assert (made / "notes.txt").read_bytes() == b"mine"


# Moving salmon's spectrals with a rename


def test_an_album_reached_through_a_link_of_another_name_has_one_tmp_dir_spectrals_folder(
    monkeypatch, dirs, tmp_path
) -> None:
    downloads, seeding = dirs
    _tmp_dir(monkeypatch, tmp_path)
    album = _album(downloads / "Album")
    (seeding / "Linked").symlink_to(album)

    assert get_spectrals_path(str(seeding / "Linked")) == get_spectrals_path(str(album))


def test_a_folder_given_another_file_before_the_second_try_is_not_deleted(monkeypatch, tmp_path, capsys) -> None:
    import shutil

    specs = tmp_path / "Album" / "Spectrals"
    specs.parent.mkdir()
    _write_spectrals(specs)
    rmtree = shutil.rmtree
    tries: list[str] = []
    slept: list[float] = []

    def held_on_the_first_try(path: str, ignore_errors: bool = False) -> None:
        # As on Windows, where a viewer holding a file stops the first try.
        tries.append(path)
        if len(tries) > 1:
            rmtree(path, ignore_errors=ignore_errors)

    async def a_file_arrives(seconds: float) -> None:
        if not slept:
            (specs / "notes.txt").write_bytes(b"theirs")
        slept.append(seconds)

    _no_upload(monkeypatch)
    monkeypatch.setattr(spectrals.shutil, "rmtree", held_on_the_first_try)
    monkeypatch.setattr(spectrals.anyio, "sleep", a_file_arrives)
    anyio.run(spectrals.handle_spectrals_upload_and_deletion, str(specs), None)

    assert (specs / "notes.txt").read_bytes() == b"theirs"
    assert len(tries) == 1
    assert "Not deleting" in capsys.readouterr().out


@pytest.mark.parametrize("put_there", ["a folder", "a link to another of salmon's"])
def test_what_takes_the_place_of_salmons_folder_during_the_delete_is_not_salmons(
    monkeypatch, tmp_path, put_there: str
) -> None:
    specs = tmp_path / "Album" / "Spectrals"
    specs.parent.mkdir()
    _write_spectrals(specs)
    other = Path(create_specs_folder(str(_album(tmp_path / "Other"))))
    slept: list[float] = []

    async def put_in_its_place(seconds: float) -> None:
        if not slept:
            if put_there == "a folder":
                specs.mkdir()
                (specs / ".theirs").write_bytes(b"theirs")
                (specs / "notes.txt").write_bytes(b"theirs")
            else:
                specs.symlink_to(other)
        slept.append(seconds)

    _no_upload(monkeypatch)
    monkeypatch.setattr(spectrals.anyio, "sleep", put_in_its_place)
    anyio.run(spectrals.handle_spectrals_upload_and_deletion, str(specs), None)

    if put_there == "a folder":
        # Left with only a hidden file of the user's, it must not pass for salmon's empty folder.
        (specs / "notes.txt").unlink()
        assert not made_by_salmon(str(specs))
    else:
        assert made_by_salmon(str(other))


def test_the_claim_goes_when_the_last_delete_try_raises(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path / "Album")
    specs = Path(create_specs_folder(str(album)))
    _fill(specs)

    def held(_path: str, ignore_errors: bool = False) -> None:
        if not ignore_errors:
            raise PermissionError("held by a viewer")

    async def no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(spectrals.shutil, "rmtree", held)
    monkeypatch.setattr(spectrals.anyio, "sleep", no_sleep)
    with pytest.raises(PermissionError):
        anyio.run(spectrals._delete_spectrals, str(specs))

    for entry in specs.iterdir():
        entry.unlink()
    # Empty, and no longer claimed: the user's.
    assert not made_by_salmon(str(specs))


def test_a_library_albums_spectrals_follow_it_to_the_renamed_copy(monkeypatch, dirs) -> None:
    downloads, _seeding = dirs
    library = downloads.parent / "library"
    library.mkdir()
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    album = _album(library / "Old Name")
    _write_spectrals(_specs(album))

    new_path = _rename(album)

    assert _files(_specs(new_path)) == SPECTRAL_FILES
    assert list(downloads.glob("spectrals_*")) == []


def test_a_second_rename_still_moves_salmons_spectrals(monkeypatch, dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Old Name")
    create_specs_folder(str(album))  # Empty, as when no spectral was made: salmon's only by its claim.
    first = _rename(album)
    monkeypatch.setattr(foldername, "generate_folder_name", lambda _metadata: "Second Name")

    second = _rename(first)

    assert not (first / "Spectrals").exists()
    assert (second / "Spectrals").is_dir()
    assert made_by_salmon(str(second / "Spectrals"))


def test_a_folder_made_later_at_the_old_path_is_not_salmons(monkeypatch, dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Old Name")
    create_specs_folder(str(album))
    _rename(album)
    (album / "Spectrals").mkdir()
    before = _listing(album)
    monkeypatch.setattr(foldername, "generate_folder_name", lambda _metadata: "Second Name")

    second = _rename(album)

    assert _listing(album) == before
    assert (second / "Spectrals").is_dir()


def test_a_folder_made_where_salmon_removed_its_own_is_the_users(monkeypatch, dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    made = create_specs_folder(str(album))
    _no_upload(monkeypatch)
    anyio.run(spectrals.handle_spectrals_upload_and_deletion, made, None)
    os.mkdir(made)

    assert not made_by_salmon(made)


def test_a_folder_of_the_new_name_holding_other_files_stops_the_rename(monkeypatch, dirs, tmp_path) -> None:
    downloads, seeding = dirs
    _tmp_dir(monkeypatch, tmp_path)
    album = _album(seeding / "Old Name")
    _write_spectrals(_specs(album))
    theirs = _specs(downloads / NEW_NAME)
    theirs.mkdir()
    (theirs / "theirs.png").write_bytes(b"theirs")

    with pytest.raises(UploadError, match="salmon did not make"):
        _rename(album)

    assert (theirs / "theirs.png").read_bytes() == b"theirs"
    assert _files(_specs(album)) == SPECTRAL_FILES


def test_a_folder_at_the_old_name_that_is_not_salmons_stays_put(monkeypatch, dirs, tmp_path) -> None:
    _downloads, seeding = dirs
    _tmp_dir(monkeypatch, tmp_path)
    album = _album(seeding / "Old Name")
    _specs(album).mkdir()
    (_specs(album) / "theirs.png").write_bytes(b"theirs")

    new_path = _rename(album)

    assert (_specs(album) / "theirs.png").read_bytes() == b"theirs"
    assert not _specs(new_path).exists()


def test_salmons_tmp_dir_spectrals_follow_a_rename_into_the_same_folder(monkeypatch, dirs, tmp_path) -> None:
    # As a case-only rename on a case-insensitive disk: the album stays where it is, its name changes.
    downloads, seeding = dirs
    tmp_dir = _tmp_dir(monkeypatch, tmp_path)
    link = seeding / "Old Name"
    link.symlink_to(_album(downloads / NEW_NAME))
    _write_spectrals(_specs(link))

    new_path = _rename(link)

    assert _files(_specs(new_path)) == SPECTRAL_FILES
    assert sorted(entry.name for entry in tmp_dir.iterdir()) == [_specs(new_path).name]


# A dry run


@pytest.mark.parametrize("with_tmp_dir", [False, True], ids=["no tmp_dir", "tmp_dir"])
def test_a_dry_run_makes_its_spectrals_outside_the_album_in_its_run_directory(
    monkeypatch, dirs, tmp_path, with_tmp_dir: bool
) -> None:
    _downloads, seeding = dirs
    tmp_dir = _tmp_dir(monkeypatch, tmp_path) if with_tmp_dir else None
    album = _album(seeding / "Album")
    (album / "Spectrals").mkdir()
    (album / "Spectrals" / "theirs.png").write_bytes(b"theirs")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with dryrun.mode(), dryrun.writing_into(str(run_dir)):
        made = create_specs_folder(str(album))

    assert Path(made).parent == run_dir
    assert (album / "Spectrals" / "theirs.png").read_bytes() == b"theirs"
    assert tmp_dir is None or list(tmp_dir.iterdir()) == []
