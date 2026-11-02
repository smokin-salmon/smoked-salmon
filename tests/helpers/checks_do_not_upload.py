"""Helpers of tests/test_checks_do_not_upload.py that other test modules use too."""

from pathlib import Path

from salmon.checks import do_not_upload
from salmon.checks.do_not_upload import LISTS

# A list that reads, for a tracker a test gives no list: an empty one forbids everything.
UNMATCHED = "[[entry]]\nartist = 'test-artist-unmatched'\nnote = 'Matches no test release.'\n"


def write_lists(monkeypatch, folder: Path, **lists: str) -> None:
    """Make salmon read its lists from folder: the TOML text given for each tracker, and UNMATCHED otherwise."""
    folder.mkdir(exist_ok=True)
    for tracker, name in LISTS.items():
        (folder / name).write_text(lists.get(tracker, UNMATCHED), encoding="utf-8")
    monkeypatch.setattr(do_not_upload, "LISTS_DIR", folder)
