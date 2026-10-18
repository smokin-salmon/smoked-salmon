"""clean_tmp_dir leaves alone what a running salmon has in tmp_dir.

Every salmon holds a shared flock on tmp_dir/.salmon.lock for its whole life; the one that cleans needs the
exclusive lock first. flock locks taken through two separate open() calls conflict even within one process, so
a second open file holding a shared lock stands for another salmon here.
"""

import os
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import salmon.run
from salmon import cfg
from salmon.common import tmp_lock

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

# Only the tests that take a real flock need fcntl; the fallback ones run everywhere.
needs_flock = pytest.mark.skipif(fcntl is None, reason="no flock here")

DAY = 24 * 60 * 60


@pytest.fixture
def tmp_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    folder = tmp_path / "tmp"
    folder.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(folder))
    monkeypatch.setattr(cfg.directory, "clean_tmp_dir", True)
    yield folder
    tmp_lock.release()


def _entries(folder: Path) -> list[str]:
    return sorted(os.listdir(folder))


def _fill(folder: Path) -> None:
    (folder / "Album-spectrals").mkdir()
    (folder / "Album-spectrals" / "01 Zoom.png").write_bytes(b"png")
    (folder / "salmon-web-1-specs-abc").mkdir()
    (folder / "leftover.txt").write_text("x")


def _blocked(path: Path, operation: int) -> bool:
    """Whether a new open file of path cannot take this lock now."""
    assert fcntl is not None
    with open(path) as probe:
        try:
            fcntl.flock(probe, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


@needs_flock
def test_cleanup_leaves_a_running_salmons_files(tmp_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert fcntl is not None
    _fill(tmp_dir)
    with open(tmp_dir / ".salmon.lock", "a") as other:
        fcntl.flock(other, fcntl.LOCK_SH)

        salmon.run.cleanup_tmp_dir()

    assert _entries(tmp_dir) == [".salmon.lock", "Album-spectrals", "leftover.txt", "salmon-web-1-specs-abc"]
    out = capsys.readouterr().out
    assert "Another salmon is running: not cleaning tmp_dir." in out
    assert "Cleaned temporary directory" not in out


@needs_flock
def test_cleanup_alone_cleans_everything_but_the_lock_then_holds_it_shared(
    tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fcntl is not None
    _fill(tmp_dir)
    (tmp_dir / "link").symlink_to(tmp_dir / "leftover.txt")

    salmon.run.cleanup_tmp_dir()

    assert _entries(tmp_dir) == [".salmon.lock"]
    assert "Cleaned temporary directory" in capsys.readouterr().out
    # This salmon now holds a shared lock: another may share it, but cannot clean.
    exclusive_blocked = _blocked(tmp_dir / ".salmon.lock", fcntl.LOCK_EX)
    shared_blocked = _blocked(tmp_dir / ".salmon.lock", fcntl.LOCK_SH)
    assert exclusive_blocked
    assert not shared_blocked


@needs_flock
def test_a_second_salmon_started_later_does_not_clean_the_first_ones_files(tmp_dir: Path) -> None:
    salmon.run.cleanup_tmp_dir()
    _fill(tmp_dir)
    first = tmp_lock.detach()

    try:
        salmon.run.cleanup_tmp_dir()
    finally:
        if first is not None:
            first.close()

    assert _entries(tmp_dir) == [".salmon.lock", "Album-spectrals", "leftover.txt", "salmon-web-1-specs-abc"]


@needs_flock
def test_no_clean_tmp_dir_deletes_nothing_but_still_takes_the_shared_lock(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fcntl is not None
    monkeypatch.setattr(cfg.directory, "clean_tmp_dir", False)
    _fill(tmp_dir)

    salmon.run.cleanup_tmp_dir()

    assert _entries(tmp_dir) == [".salmon.lock", "Album-spectrals", "leftover.txt", "salmon-web-1-specs-abc"]
    assert capsys.readouterr().out == ""
    exclusive_blocked = _blocked(tmp_dir / ".salmon.lock", fcntl.LOCK_EX)
    assert exclusive_blocked


def test_no_tmp_dir_takes_no_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "clean_tmp_dir", True)

    salmon.run.cleanup_tmp_dir()

    held = tmp_lock.detach()
    if held is not None:
        held.close()
    assert held is None


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _old_and_new(folder: Path) -> None:
    (folder / "old.txt").write_text("x")
    _age(folder / "old.txt", DAY + 60)
    (folder / "new.txt").write_text("x")
    # A folder that looks old but holds a file changed just now: a running salmon's.
    (folder / "busy").mkdir()
    (folder / "busy" / "fresh.png").write_bytes(b"png")
    _age(folder / "busy", 2 * DAY)
    # A folder whose every entry is old.
    (folder / "stale").mkdir()
    (folder / "stale" / "inner").mkdir()
    (folder / "stale" / "inner" / "old.png").write_bytes(b"png")
    for path in (folder / "stale" / "inner" / "old.png", folder / "stale" / "inner", folder / "stale"):
        _age(path, 2 * DAY)


def test_without_fcntl_only_entries_unchanged_for_a_day_go(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tmp_lock, "fcntl", None)
    _old_and_new(tmp_dir)

    salmon.run.cleanup_tmp_dir()

    assert _entries(tmp_dir) == ["busy", "new.txt"]
    out = capsys.readouterr().out
    assert out.count("24 hours") == 1
    assert "Cleaned temporary directory" in out


@needs_flock
def test_a_lock_file_that_cannot_be_locked_falls_back_to_the_age_rule(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_locks(_file: object, _operation: int) -> None:
        raise OSError(37, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)
    _old_and_new(tmp_dir)
    (tmp_dir / ".salmon.lock").write_text("")
    _age(tmp_dir / ".salmon.lock", 2 * DAY)

    salmon.run.cleanup_tmp_dir()

    assert _entries(tmp_dir) == [".salmon.lock", "busy", "new.txt"]
    assert capsys.readouterr().out.count("24 hours") == 1


def test_a_lock_file_that_cannot_be_created_falls_back_to_the_age_rule(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_dir / ".salmon.lock").mkdir()
    _old_and_new(tmp_dir)

    salmon.run.cleanup_tmp_dir()

    assert _entries(tmp_dir) == [".salmon.lock", "busy", "new.txt"]
    assert capsys.readouterr().out.count("24 hours") == 1


@needs_flock
def test_a_salmon_cleaning_for_too_long_is_waited_for_a_bounded_time(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fcntl is not None
    monkeypatch.setattr(tmp_lock, "WAIT_SECONDS", 0.3)
    _old_and_new(tmp_dir)
    with open(tmp_dir / ".salmon.lock", "a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        started = time.monotonic()

        salmon.run.cleanup_tmp_dir()

        assert time.monotonic() - started < 5

    assert _entries(tmp_dir) == [".salmon.lock", "busy", "new.txt"]
    assert capsys.readouterr().out.count("24 hours") == 1


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs folder permissions that bind (not root)")
def test_without_a_lock_a_folder_that_cannot_be_read_whole_is_kept(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tmp_lock, "fcntl", None)
    removed: list[str] = []
    remove = salmon.run._remove

    def spy(path: str) -> bool:
        removed.append(os.path.basename(path))
        return remove(path)

    monkeypatch.setattr(salmon.run, "_remove", spy)
    stale = tmp_dir / "stale"
    (stale / "hidden").mkdir(parents=True)
    # Changed just now, but out of sight: the folder's age cannot be told.
    (stale / "hidden" / "fresh.png").write_bytes(b"png")
    _age(stale / "hidden", 2 * DAY)
    _age(stale, 2 * DAY)
    (stale / "hidden").chmod(0)
    try:
        salmon.run.cleanup_tmp_dir()
    finally:
        (stale / "hidden").chmod(0o755)

    assert removed == []
    assert (stale / "hidden" / "fresh.png").exists()
    out = capsys.readouterr().out
    assert "Cleaned temporary directory partly: 1 entry left in" in out
    assert "Cleaned temporary directory:" not in out


@needs_flock
def test_a_failed_removal_is_not_reported_as_a_clean_tmp_dir(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fill(tmp_dir)
    rmtree = shutil.rmtree

    def failing_rmtree(path: str, *args: object, **kwargs: object) -> None:
        if os.path.basename(path) == "Album-spectrals":
            raise PermissionError(13, "Permission denied", path)
        rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)

    salmon.run.cleanup_tmp_dir()

    assert _entries(tmp_dir) == [".salmon.lock", "Album-spectrals"]
    out = capsys.readouterr().out
    assert "Failed to remove" in out
    assert "Cleaned temporary directory partly: 1 entry left in" in out
    assert "Cleaned temporary directory:" not in out


@needs_flock
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_a_fifo_at_the_lock_name_does_not_hang_startup(tmp_dir: Path) -> None:
    fifo = tmp_dir / tmp_lock.LOCK_NAME
    os.mkfifo(fifo)
    claims: list[tmp_lock.Claim] = []
    thread = threading.Thread(target=lambda: claims.append(tmp_lock.claim(str(tmp_dir), exclusive=True)), daemon=True)
    thread.start()
    thread.join(5)
    hung = thread.is_alive()
    if hung:
        # Open the FIFO's other end so the stuck open() returns and the thread ends.
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        thread.join(5)
        os.close(reader)
    assert not hung
    assert claims == [tmp_lock.Claim.UNLOCKED]

    # With a reader at the other end the open succeeds, and a FIFO is still no lock file.
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        claimed = tmp_lock.claim(str(tmp_dir), exclusive=True)
    finally:
        os.close(reader)
    assert claimed is tmp_lock.Claim.UNLOCKED
