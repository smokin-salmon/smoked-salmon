"""`salmon up` on an album holding something it cannot read or remove (#950): a message naming it, exit 1, before
any tracker request, with nothing in the album changed.

The runs go through run.main, as the command line does, against the local fake tracker of helpers.uploader_dry_run.
"""

import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers.uploader_dry_run import FakeTracker, _album, _lossy_with_spectrals, _snapshot
from test_run_exit_status import (
    Row,
    _main_up,
    no_waits,  # noqa: F401 (a fixture: see pytestmark)
)
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

import salmon.uploader.spectrals
from salmon import cfg
from salmon.uploader import staging

pytestmark = pytest.mark.usefixtures("image_uploads", "no_waits")

NEEDS_PERMISSIONS = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs file permissions that bind (not root)"
)
WHAT_TO_DO = "fix it or remove it from the album, then run salmon again."


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a dot_torrents_dir, configured, as in test_run_exit_status."""
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return downloads, torrents


# Each makes the bad entry in the album and gives what puts the album back as it was.
Make = Callable[[Path], Callable[[], None]]


def _scans(album: Path, mode: int) -> Callable[[], None]:
    (album / "Scans").mkdir()
    (album / "Scans" / "a.jpg").write_bytes(b"jpeg")
    os.chmod(album / "Scans", mode)
    return lambda: os.chmod(album / "Scans", 0o755)


def _folder_444(album: Path) -> Callable[[], None]:
    return _scans(album, 0o444)


def _folder_000(album: Path) -> Callable[[], None]:
    return _scans(album, 0)


def _broken_link(album: Path) -> Callable[[], None]:
    (album / "gone.jpg").symlink_to(album / "not-there.jpg")
    return (album / "gone.jpg").unlink


def _self_link(album: Path) -> Callable[[], None]:
    (album / "loop").symlink_to(album / "loop")
    return (album / "loop").unlink


UNREADABLE = [
    pytest.param(_folder_444, os.path.join("Scans", "a.jpg"), id="folder-444", marks=NEEDS_PERMISSIONS),
    pytest.param(_folder_000, "Scans", id="folder-000", marks=NEEDS_PERMISSIONS),
    pytest.param(_broken_link, "gone.jpg", id="broken-link"),
    pytest.param(_self_link, "loop", id="self-link"),
]


def _up(
    monkeypatch, capsys, dirs: tuple[Path, Path], make: Make, name: str, row: Row
) -> tuple[object, str, FakeTracker, Path]:
    """Run `salmon up` on an album holding what `make` makes, `name` in it; check that nothing in the album went
    and that no run directory stays."""
    downloads, torrents = dirs
    album = _album(downloads.parent / "inbox" / "Album")
    before = _snapshot(album)
    undo = make(album)
    tracker = row.tracker()
    top = name.split(os.sep)[0]
    try:
        code, out = _main_up(monkeypatch, capsys, album, torrents, row, tracker)
        assert os.path.lexists(album / top), out
    finally:
        undo()
    assert top == name or os.path.lexists(album / name), out

    def others(snapshot: dict[str, tuple[str, int, int]]) -> dict[str, tuple[str, int, int]]:
        return {key: value for key, value in snapshot.items() if key.split(os.sep)[0] != top}

    assert others(_snapshot(album)) == others(before), out
    staging_root = downloads / staging.STAGING_DIR
    assert not staging_root.exists() or os.listdir(staging_root) == []
    return code, out, tracker, album


@pytest.mark.parametrize(("make", "name"), UNREADABLE)
@pytest.mark.parametrize("args", [pytest.param((), id="up"), pytest.param(("--dry-run",), id="dry-run")])
def test_an_entry_it_cannot_read_is_named_before_any_request(
    monkeypatch, capsys, dirs, make: Make, name: str, args: tuple[str, ...]
) -> None:
    code, out, tracker, album = _up(monkeypatch, capsys, dirs, make, name, Row(1, 0, "", args=args))

    assert code == 1, out
    assert tracker.sent == []
    # Named from the album, with the reason the system gives.
    assert re.search(
        rf"There was an error: Could not read {re.escape(name)} in {re.escape(str(album))} \([^)]+\): ", out
    )
    assert WHAT_TO_DO in out
    assert "Traceback" not in out
    # A folder it cannot list is no reason to say the album shares files.
    assert "shares files" not in out


SALMONS = ("01 Full.png", "01 Zoom.png")


def _read_only_spectrals(album: Path) -> Callable[[], None]:
    """salmon's spectrals an earlier run left, in a folder whose files cannot be removed."""
    (album / "Spectrals").mkdir()
    for image in SALMONS:
        (album / "Spectrals" / image).write_bytes(b"png")
    os.chmod(album / "Spectrals", 0o555)
    return lambda: os.chmod(album / "Spectrals", 0o755)


@NEEDS_PERMISSIONS
@pytest.mark.parametrize(
    ("tmp_dir", "args"),
    [
        pytest.param(True, (), id="tmp_dir"),
        pytest.param(False, (), id="no-tmp_dir"),
        pytest.param(False, ("-a",), id="spectrals-after"),
    ],
)
def test_a_leftover_spectrals_folder_it_cannot_remove_is_named_before_any_request(
    monkeypatch, capsys, dirs, tmp_path, tmp_dir: bool, args: tuple[str, ...]
) -> None:
    if tmp_dir:
        (tmp_path / "tmp_dir").mkdir()
        monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_path / "tmp_dir"))
    monkeypatch.setattr(salmon.uploader.spectrals, "check_spectrals", _lossy_with_spectrals)

    code, out, tracker, album = _up(
        monkeypatch, capsys, dirs, _read_only_spectrals, os.path.join("Spectrals", SALMONS[0]), Row(1, 0, "", args=args)
    )

    assert code == 1, out
    assert tracker.sent == []
    # The folder, or the file in it the system names.
    assert re.search(rf"There was an error: Could not remove Spectrals.*? in {re.escape(str(album))} \([^)]+\): ", out)
    assert "Traceback" not in out
    assert sorted(os.listdir(album / "Spectrals")) == sorted(SALMONS)


# What already ended well, and still does


def test_as_before_a_readable_album_goes_up(monkeypatch, capsys, dirs) -> None:
    downloads, torrents = dirs
    album = _album(downloads.parent / "inbox" / "Album")
    tracker = FakeTracker()

    code, out = _main_up(monkeypatch, capsys, album, torrents, Row(0, 1, "", args=("--conversions", "none")), tracker)

    assert code == 0, out
    # The fake tracker records what the runs above did not send.
    assert sum(sent.query.get("action") == "upload" for sent in tracker.sent) == 1
    assert "Could not" not in out


@NEEDS_PERMISSIONS
def test_as_before_a_file_it_cannot_read_stops_a_dry_run_at_the_copy(monkeypatch, capsys, dirs) -> None:
    def unreadable_cover(album: Path) -> Callable[[], None]:
        os.chmod(album / "cover.jpg", 0)
        return lambda: os.chmod(album / "cover.jpg", 0o644)

    code, out, tracker, album = _up(
        monkeypatch, capsys, dirs, unreadable_cover, "cover.jpg", Row(1, 0, "", args=("--dry-run",))
    )

    assert code == 1, out
    assert tracker.sent == []
    assert f"There was an error: Could not copy {album} to " in out
