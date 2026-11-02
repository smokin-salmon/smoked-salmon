"""Helpers of tests/test_webui_pages.py that other test modules use too."""

from dataclasses import dataclass
from pathlib import Path

from mutagen.flac import FLAC

from helpers.checks_mqa import _write_flac


@dataclass
class Roots:
    downloads: Path
    library: Path
    outside: Path
    scratch: Path


def _album(folder: Path, tracks: int = 2) -> Path:
    folder.mkdir(parents=True)
    for number in range(1, tracks + 1):
        track = folder / f"{number:02d} Track {number}.flac"
        _write_flac(track, mqa_marker=False)
        tags = FLAC(track)
        tags.update({"artist": "Artist", "album": "Album", "date": "2020", "title": f"Track {number}"})
        tags["tracknumber"] = str(number)
        tags.save()
    return folder


def _refused_paths(roots: Roots) -> dict[str, tuple[str, int]]:
    """Each folder the rule refuses, and the status it is refused with."""
    _album(roots.outside / "Album")
    album = _album(roots.downloads / "Album")
    (roots.downloads / "out").symlink_to(roots.outside / "Album")
    (roots.downloads / "linked").symlink_to(album)
    (roots.downloads / "empty").mkdir()
    (roots.library / "into").symlink_to(roots.downloads)
    return {
        "outside the roots": (str(roots.outside / "Album"), 403),
        "..": (f"{roots.downloads}/../outside/Album", 403),
        "encoded ..": (f"{roots.downloads}/%2e%2e/outside/Album", 404),
        "a symlink out of a root": (str(roots.downloads / "out"), 403),
        "a root": (str(roots.downloads), 403),
        "a library root": (str(roots.library), 403),
        "a symlinked album folder": (str(roots.downloads / "linked"), 403),
        "a folder linked into a library from outside": (str(roots.library / "into" / "Album"), 403),
        "a folder with no audio": (str(roots.downloads / "empty"), 422),
        "a relative path": ("downloads/Album", 400),
        "a missing folder": (str(roots.downloads / "missing"), 404),
    }
