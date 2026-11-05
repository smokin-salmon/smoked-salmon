"""get_audio_files: the order doesn't depend on the disk, and a name is relative to the album however its path is given.

A tie is shown by comparing the order as found with os.walk reversed: a tie left to the disk differs on any filesystem.
Names differing only in case go through a made-up walk, as they can't both exist on a filesystem that ignores case.
"""

import itertools
import os
import random
from collections import defaultdict
from pathlib import Path

import pytest

from salmon.common.files import get_audio_files


def _album(root: Path, *names: str) -> str:
    """A real folder holding these (empty) files, its path as a string."""
    for name in names:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    return str(root)


def _reverse_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.walk gives what it finds last first: the folders, and the files in each."""
    walk = os.walk

    def reversed_walk(top: str, **kwargs):
        for root, folders, files in reversed(list(walk(top, **kwargs))):
            yield root, folders[::-1], files[::-1]

    monkeypatch.setattr(os, "walk", reversed_walk)


def _walk_finding(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """os.walk finds these names in one folder, in this order, and nothing is on disk."""
    monkeypatch.setattr(os, "walk", lambda top, **_: iter([(top, [], list(names))]))


def _walk_shuffled(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    """os.walk finds these paths, the folders and the files in each in a shuffled order, and nothing is on disk."""
    by_folder: dict[str, list[str]] = defaultdict(list)
    for name in names:
        folder, file = os.path.split(name)
        by_folder[folder].append(file)
    rng = random.Random(940)
    folders = rng.sample(sorted(by_folder), len(by_folder))

    def walk(top: str, **_):
        for folder in folders:
            yield os.path.join(top, folder).rstrip(os.sep), [], rng.sample(by_folder[folder], len(by_folder[folder]))

    monkeypatch.setattr(os, "walk", walk)


# The 126 padded layouts quoted on #940, rebuilt: 3 styles x 6 disc counts x 7 track counts. The generator behind that
# number is gone, so this list is a reconstruction of it, from its description, and not proven the same one. 115 of the
# 126 are distinct: flat gives the same names for the same disc count times track count.
STYLES = ("flat", "CD01/", "1.01")
DISC_COUNTS = (1, 2, 3, 9, 10, 12)
TRACK_COUNTS = (1, 2, 9, 10, 12, 99, 100)
PADDED_LAYOUTS = list(itertools.product(STYLES, DISC_COUNTS, TRACK_COUNTS))


def _padded(style: str, discs: int, tracks: int) -> list[str]:
    """A layout's names, in the order they're generated: each number padded to the width of the layout's largest."""
    disc_width, track_width = len(str(discs)), len(str(tracks))
    names = []
    for disc, track in itertools.product(range(1, discs + 1), range(1, tracks + 1)):
        name = f"{track:0{track_width}d} Title.flac"
        if style == "CD01/":
            names.append(f"CD{disc:0{disc_width}d}/{name}")
        elif style == "1.01":
            names.append(f"{disc:0{disc_width}d}.{name}")
        else:
            names.append(f"{(disc - 1) * tracks + track:0{len(str(discs * tracks))}d} Title.flac")
    return names


@pytest.mark.parametrize(("style", "discs", "tracks"), PADDED_LAYOUTS)
def test_as_before_padded_layouts_keep_their_order(monkeypatch, style, discs, tracks) -> None:
    names = _padded(style, discs, tracks)
    _walk_shuffled(monkeypatch, names)
    assert get_audio_files("album") == names


# The tie: two paths starting with the same number.


@pytest.mark.parametrize(
    "names",
    [
        ["01 a.flac", "01 b.flac"],
        ["1-01 One.flac", "1-02 Two.flac", "1-03 Three.flac"],
        ["1/01.flac", "1/02.flac", "1/03.flac"],
    ],
)
def test_as_before_a_tie_on_the_number_goes_by_name_not_disk_order(tmp_path, monkeypatch, names):
    path = _album(tmp_path, *names)
    as_found = get_audio_files(path)
    _reverse_walk(monkeypatch)
    assert get_audio_files(path) == as_found == names


def test_a_tie_on_the_number_puts_the_folder_first(tmp_path, monkeypatch):
    names = ["1/01.flac", "1 a.flac", "01.flac"]
    path = _album(tmp_path, *names)
    as_found = get_audio_files(path)
    _reverse_walk(monkeypatch)
    assert get_audio_files(path) == as_found == names


def test_a_tie_breaks_inside_its_number_only(tmp_path, monkeypatch):
    path = _album(tmp_path, "1-02.flac", "1-01.flac", "2-02.flac", "2-01.flac", "10-01.flac")
    as_found = get_audio_files(path)
    _reverse_walk(monkeypatch)
    assert get_audio_files(path) == as_found == ["1-01.flac", "1-02.flac", "2-01.flac", "2-02.flac", "10-01.flac"]


def test_as_before_numbered_names_differing_only_in_case_keep_one_order(monkeypatch):
    _walk_finding(monkeypatch, "01 a.flac", "01 A.flac")
    assert get_audio_files("album") == ["01 A.flac", "01 a.flac"]
    _walk_finding(monkeypatch, "01 A.flac", "01 a.flac")
    assert get_audio_files("album") == ["01 A.flac", "01 a.flac"]


def test_as_before_unnumbered_names_differing_only_in_case_keep_one_order(monkeypatch):
    _walk_finding(monkeypatch, "track.flac", "Track.flac")
    assert get_audio_files("album") == ["Track.flac", "track.flac"]
    _walk_finding(monkeypatch, "Track.flac", "track.flac")
    assert get_audio_files("album") == ["Track.flac", "track.flac"]


def test_a_folder_comes_before_its_longer_named_sibling(tmp_path):
    path = _album(tmp_path, "CD1/01.flac", "CD1 Bonus/01.flac", "CD2/01.flac")
    assert get_audio_files(path) == ["CD1/01.flac", "CD1 Bonus/01.flac", "CD2/01.flac"]


# As before: these pass on the code before the fix too.
