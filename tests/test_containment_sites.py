"""Each caller of is_within: which way round it asks, and what its answer does there."""

import ntpath
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from salmon import cfg
from salmon.converter import conversions
from salmon.cross_upload import CrossUploadRefused, _verify_release_files
from salmon.uploader import seedbox, spectrals, staging
from salmon.webui import paths

# Spelled from the tree's root, and whether it resolves to Music or inside it.
RESOLVED = [
    ("Music", True),
    ("Music/Album", True),
    ("Music/Album/Disc 1/", True),
    ("Music/./Album", True),
    ("elsewhere/in", True),
    ("Music-old/Album", False),
    (".", False),
    ("Music/out", False),
    ("Music/../elsewhere", False),
    ("MUSIC/Album", False),
]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "tree"
    (root / "Music" / "Album" / "Disc 1").mkdir(parents=True)
    (root / "Music-old" / "Album").mkdir(parents=True)
    (root / "elsewhere").mkdir()
    (root / "Music" / "out").symlink_to(root / "elsewhere", target_is_directory=True)
    (root / "elsewhere" / "in").symlink_to(root / "Music" / "Album", target_is_directory=True)
    (root / "elsewhere" / "music").symlink_to(root / "Music", target_is_directory=True)
    return root


def _spelled(tree: Path, spelled: str) -> str:
    return os.path.join(tree, spelled)


# --- A dry run's record, and the album a copy is of ----------------------------------------------


@pytest.mark.parametrize(("spelled", "inside"), RESOLVED)
def test_as_before_a_dry_run_writes_a_record_only_inside_its_scratch(tree, monkeypatch, spelled, inside) -> None:
    monkeypatch.setattr(conversions.dryrun, "scratch_dir", lambda: str(tree / "elsewhere" / "music"))

    assert conversions._inside_scratch(_spelled(tree, spelled)) is inside


def test_a_record_on_another_windows_drive_is_outside_the_scratch_directory(monkeypatch) -> None:
    monkeypatch.setattr(conversions.dryrun, "scratch_dir", lambda: "C:\\scratch")
    with monkeypatch.context() as windows:  # Only around the call: pytest uses these too, to report a failure.
        windows.setattr(os.path, "commonpath", ntpath.commonpath)
        windows.setattr(os.path, "realpath", lambda path, **_kwargs: path)
        inside = conversions._inside_scratch("D:\\Album")

    assert not inside


@pytest.mark.parametrize(("spelled", "inside"), RESOLVED)
def test_as_before_a_conversion_of_a_runs_copy_is_of_the_album(tree, spelled, inside) -> None:
    path = _spelled(tree, spelled)

    with conversions.copies_of("/albums/Original", str(tree / "elsewhere" / "music")):
        found = conversions.source_of(path)

    assert found == ("/albums/Original" if inside else path)


# --- Removing the local copy ---------------------------------------------------------------------


@pytest.mark.parametrize(("spelled", "inside"), RESOLVED)
def test_as_before_only_a_real_folder_inside_download_directory_may_be_removed(
    tree, monkeypatch, spelled, inside
) -> None:
    monkeypatch.setattr(seedbox.cfg.directory, "download_directory", str(tree / "elsewhere" / "music"))
    monkeypatch.setattr(seedbox.cfg.directory, "library_dirs", [])

    reason = seedbox.UploadManager()._kept_reason(_spelled(tree, spelled))

    # download_directory itself is not inside it; a folder given as a link is left alone by its own check.
    allowed = inside and spelled not in ("Music", "elsewhere/in")
    assert ("not a folder inside download_directory" not in str(reason)) is allowed


# --- The scratch copy's removal: a True refuses the rmtree ---------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    downloads = tmp_path.resolve() / "downloads"
    run = downloads / staging.STAGING_DIR / "run"
    (run / "Album").mkdir(parents=True)
    monkeypatch.setattr(staging.cfg.directory, "download_directory", str(downloads))
    return run


def test_as_before_the_run_directory_holding_the_source_stays(run_dir: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(run_dir / "Album", target_is_directory=True)
    for source in (run_dir, run_dir / "Album", link):
        staging._remove_scratch_dir(str(run_dir), str(source))

        assert run_dir.is_dir(), source


def test_as_before_a_run_directory_reached_through_a_linked_folder_stays_when_it_holds_the_source(
    run_dir: Path, tmp_path: Path
) -> None:
    linked = tmp_path / "linked"
    linked.symlink_to(run_dir.parent, target_is_directory=True)

    staging._remove_scratch_dir(str(linked / "run"), str(run_dir / "Album"))

    assert run_dir.is_dir()


@pytest.mark.parametrize("source", ["../run-old/Album", "..", "../.."], ids=["a sibling", "its parent", "above that"])
def test_as_before_a_run_directory_not_holding_the_source_is_removed(run_dir: Path, source: str) -> None:
    (run_dir.parent / "run-old" / "Album").mkdir(parents=True)

    staging._remove_scratch_dir(str(run_dir), os.path.normpath(run_dir / source))

    assert not run_dir.exists()


# --- Moving spectrals out of the album -----------------------------------------------------------


def _spectral_uploads(path: str, album: str) -> spectrals.SpectralUploads:
    uploads = spectrals.SpectralUploads.__new__(spectrals.SpectralUploads)
    uploads._scratch = None
    uploads._path = path
    uploads._album_path = album
    return uploads


@pytest.mark.parametrize(("spelled", "inside"), RESOLVED)
def test_as_before_spectrals_move_out_only_when_they_resolve_inside_the_album(
    tree, tmp_path, monkeypatch, spelled, inside
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(spectrals.tempfile, "mkdtemp", lambda **_kwargs: str(scratch))
    uploads = _spectral_uploads(_spelled(tree, spelled), str(tree / "elsewhere" / "music"))

    uploads._keep_out_of_album()

    assert (uploads._scratch is not None) is inside


# --- Cross-upload's link check: a False refuses --------------------------------------------------


def _listing(names: list[str]) -> dict:
    return {"torrent": {"fileList": "|||".join(f"{name}{{{{{{4}}}}}}" for name in names)}}


@pytest.mark.parametrize(
    ("target", "refused"),
    [
        ("Album/kept.txt", False),
        ("Album", False),
        ("Album-old/kept.txt", True),
        ("kept.txt", True),
    ],
    ids=["a file in the album", "the album itself", "a sibling with the same start", "above the album"],
)
def test_as_before_a_cross_upload_refuses_a_link_out_of_the_album(tmp_path, target: str, refused: bool) -> None:
    root = tmp_path.resolve()
    for folder in ("Album", "Album-old"):
        (root / folder).mkdir()
        (root / folder / "kept.txt").write_bytes(b"data")
    (root / "kept.txt").write_bytes(b"data")
    (root / "Album" / "link").symlink_to(root / target)
    names = ["kept.txt"] if target == "Album" else ["kept.txt", "link"]
    rules = SimpleNamespace(TAG_RULES=SimpleNamespace(max_path_length=180), site_string="RED")

    if refused:
        with pytest.raises(CrossUploadRefused, match="link is a link out of the album folder"):
            _verify_release_files(_listing(names), root / "Album", rules)  # pyright: ignore[reportArgumentType]
    else:
        _verify_release_files(_listing(names), root / "Album", rules)  # pyright: ignore[reportArgumentType]


# --- salmon web ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spelled", "inside"),
    [
        ("Music", True),
        ("Music/Album", True),
        ("Music-old/Album", False),
        (".", False),
    ],
)
def test_as_before_the_web_confines_a_path_to_the_roots(tree, spelled: str, inside: bool) -> None:
    found = [paths.Root(str(tree / "Music"), False)]

    assert paths._within(os.path.normpath(_spelled(tree, spelled)), found) is inside


@pytest.mark.parametrize("name", ["MUSIC", "elsewhere/music"], ids=["another case", "a link's name"])
def test_as_before_confinement_goes_by_the_text_alone(tree, name: str) -> None:
    root = tree / "Music"
    other = tree / name
    if name == "MUSIC" and not other.exists():
        other.mkdir()

    assert not paths._within(str(other), [paths.Root(str(root), False)])
    assert not paths._within(str(other / "Album"), [paths.Root(str(root), False)])


def test_as_before_the_withheld_check_sees_a_second_name_that_confinement_does_not(tree) -> None:
    second_name = str(tree / "elsewhere" / "music")

    assert paths._same_or_inside(second_name, str(tree / "Music"))
    assert not paths._within(second_name, [paths.Root(str(tree / "Music"), False)])


@pytest.mark.parametrize(
    ("given", "named"),
    [
        ("elsewhere/music/Album", True),
        ("Music/Album", True),
        ("Music", True),
        ("elsewhere", False),
        (".", False),
        ("Music-old/Album", False),
    ],
)
def test_as_before_a_path_given_in_a_library_is_named_by_either_spelling(tree, monkeypatch, given, named) -> None:
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(tree / "elsewhere" / "music")])

    assert paths._named_in_library(os.path.normpath(_spelled(tree, given))) is named
