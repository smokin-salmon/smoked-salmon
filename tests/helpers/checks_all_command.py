"""Helpers of tests/test_checks_all_command.py that other test modules use too."""

import builtins
import errno
import os
from pathlib import Path

import pytest

# A stand-in for flac's test mode: a file that is a FLAC stream decodes, anything else fails as flac fails.
FAKE_FLAC = """\
import sys
path = sys.argv[-1]
with open(path, "rb") as handle:
    good = handle.read(4) == b"fLaC"
sys.stderr.write(f"{path}: ok\\n" if good else f"{path}: ERROR while decoding data\\n")
sys.exit(0 if good else 1)
"""


def _snapshot(path: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns) for p in path.rglob("*") if p.is_file()
    ) + [("", 0, path.stat().st_mtime_ns)]


def _fail_scandir(monkeypatch: pytest.MonkeyPatch, error: type[OSError], *folders: str) -> None:
    """Make os.scandir raise this error on the folders of these names, without permissions, so for root too."""
    real_scandir = os.scandir

    def scandir(path):
        if os.path.basename(path) in folders:
            raise error(errno.EIO, "Refused by the test", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)


def _fail_lstat(monkeypatch: pytest.MonkeyPatch, error: type[OSError], *folders: str) -> None:
    """Make os.lstat raise this error on the folders of these names and what is in them, so for root too."""
    real_lstat = os.lstat

    def lstat(path, **kwargs):
        if os.path.basename(os.path.dirname(path)) in folders:
            raise error(errno.EACCES, "Refused by the test", path)
        return real_lstat(path, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)


def _fail_lstat_of(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Make os.lstat fail on these entries alone, as an odd file does in a folder that can be entered."""
    real_lstat = os.lstat

    def lstat(path, **kwargs):
        if os.path.basename(path) in names:
            raise OSError(errno.EIO, "Refused by the test", path)
        return real_lstat(path, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)


def _fail_open(monkeypatch: pytest.MonkeyPatch, error: type[OSError], *names: str) -> None:
    """Make open raise this error on the files of these names, without permissions, so for root too."""
    real_open = builtins.open

    def refusing_open(file, *args, **kwargs):
        if isinstance(file, str) and os.path.basename(file) in names:
            raise error(errno.EACCES, "Refused by the test", file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refusing_open)
