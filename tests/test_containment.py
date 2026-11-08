"""is_within: is this path inside that folder, by the text of both."""

import ntpath
import os
from pathlib import Path

import pytest

from salmon.containment import is_within


@pytest.mark.parametrize(
    ("path", "folder", "inside"),
    [
        ("/music", "/music", True),
        ("/music/Album", "/music", True),
        ("/music/Album/Disc 1", "/music", True),
        ("/music/Album", "/", True),
        ("/music-old", "/music", False),
        ("/music-old/Album", "/music", False),
        ("/", "/music", False),
        ("/mus", "/music", False),
        ("/Music/Album", "/music", False),
        ("/music/Album/", "/music", True),
        ("/music/", "/music", True),
        ("/music/./Album", "/music", True),
        # A folder written with a trailing separator is never what commonpath gives back.
        ("/music/Album", "/music/", False),
        ("/music", "/music/", False),
        # By its text this is inside: callers give resolved paths.
        ("/music/../elsewhere", "/music", True),
        ("music/Album", "music", True),
        ("music/Album", "/music", False),
        ("/music/Album", "music", False),
        ("", "/music", False),
    ],
)
def test_is_within(path: str, folder: str, inside: bool) -> None:
    assert is_within(path, folder) is inside


def test_is_within_goes_by_the_text_and_does_not_follow_a_link(tmp_path: Path) -> None:
    folder = tmp_path / "Music"
    folder.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (folder / "out").symlink_to(elsewhere, target_is_directory=True)
    (elsewhere / "in").symlink_to(folder, target_is_directory=True)

    assert is_within(str(folder / "out" / "Album"), str(folder))
    assert not is_within(str(elsewhere / "in" / "Album"), str(folder))


@pytest.mark.parametrize(
    ("path", "folder", "inside"),
    [
        ("C:\\Music\\Album", "C:\\Music", True),
        ("C:\\Music-old\\Album", "C:\\Music", False),
        ("D:\\Album", "C:\\Music", False),
        # The path's own spelling is what commonpath gives back, so another case is not inside.
        ("c:\\music\\Album", "C:\\Music", False),
    ],
)
def test_is_within_on_windows_paths(monkeypatch: pytest.MonkeyPatch, path: str, folder: str, inside: bool) -> None:
    with monkeypatch.context() as windows:  # Only around the call: pytest uses it too, to report a failure.
        windows.setattr(os.path, "commonpath", ntpath.commonpath)
        found = is_within(path, folder)

    assert found is inside
