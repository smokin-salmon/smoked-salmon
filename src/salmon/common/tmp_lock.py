"""Whether another salmon is using tmp_dir, so clean_tmp_dir leaves its files alone.

Every salmon with tmp_dir set holds a shared flock on tmp_dir/.salmon.lock for its whole life; the OS drops it
when the process ends, crash included. A salmon that cleans first needs the exclusive lock, which it gets only
when no other salmon holds one. flock locks belong to the open file, so salmons in two containers that share
the folder through a bind mount on one host see each other, where PIDs would not.

Without flock (Windows), or when the lock file cannot be made or locked, nothing tells a running salmon's files
apart, so cleaning keeps whatever changed in the last 24 hours.
"""

import enum
import os
import stat
import time
from typing import IO

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

LOCK_NAME = ".salmon.lock"
# How long to wait for a salmon that is cleaning tmp_dir right now (it holds the lock exclusively meanwhile).
WAIT_SECONDS = 60.0
_POLL_SECONDS = 0.1

_held: IO[str] | None = None


class Claim(enum.Enum):
    ALONE = "alone"  # No other salmon holds the lock: tmp_dir may be cleaned.
    SHARED = "shared"  # Another salmon holds it (or exclusive was not asked for): clean nothing.
    UNLOCKED = "unlocked"  # No flock here, or the lock file cannot be made or locked.


def _wait_for(file: IO[str], operation: int) -> bool:
    """Take the lock, waiting a bounded time for a salmon that holds it exclusively. Raises OSError but on timeout."""
    assert fcntl is not None
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            fcntl.flock(file, operation | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SECONDS)


def claim(tmp_dir: str, exclusive: bool) -> Claim:
    """Take this process's lock on tmp_dir, held until it ends.

    With exclusive, try for the exclusive lock first: ALONE means it was got, and the caller cleans then calls
    share(). Otherwise the lock is shared, as every salmon holds it.
    """
    global _held
    release()
    if fcntl is None:
        return Claim.UNLOCKED
    try:
        # Non-blocking, so a FIFO planted at the lock's name cannot hang startup (no reader: ENXIO).
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(os.path.join(tmp_dir, LOCK_NAME), flags, 0o644)
    except OSError:
        return Claim.UNLOCKED
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return Claim.UNLOCKED
        # Kept open for the process's life: closing it drops the lock.
        file = os.fdopen(fd, "a", encoding="utf-8")
    except OSError:
        os.close(fd)
        return Claim.UNLOCKED
    try:
        if exclusive:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _held = file
                return Claim.ALONE
            except BlockingIOError:
                pass
        if not _wait_for(file, fcntl.LOCK_SH):
            file.close()
            return Claim.UNLOCKED
    except OSError:
        file.close()
        return Claim.UNLOCKED
    _held = file
    return Claim.SHARED


def share() -> bool:
    """Turn the exclusive lock claim() got into the shared one every running salmon holds; False if lost."""
    global _held
    if _held is None or fcntl is None:
        return False
    try:
        # Not atomic: another salmon may take the exclusive lock in between and clean, which is harmless
        # before this one has made anything there; this then waits for it to finish.
        if _wait_for(_held, fcntl.LOCK_SH):
            return True
    except OSError:
        pass
    _held.close()
    _held = None
    return False


def release() -> None:
    """Drop this process's lock (tests; a process's own end drops it anyway)."""
    file = detach()
    if file is not None:
        file.close()


def detach() -> IO[str] | None:
    """Hand over the open lock file, still locked, and forget it here."""
    global _held
    file, _held = _held, None
    return file


def _raise(error: OSError) -> None:
    raise error


def newest_change(path: str) -> float:
    """The latest mtime of path and, for a folder, of everything in it (symlinks not followed).

    Raises OSError when part of it cannot be read: its age is then unknown.
    """
    newest = os.lstat(path).st_mtime
    if os.path.isdir(path) and not os.path.islink(path):
        for root, dirs, files in os.walk(path, onerror=_raise):
            for name in dirs + files:
                newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
    return newest
