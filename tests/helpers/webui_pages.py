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
    _album(roots.downloads / "incoming" / "Store" / "Album")
    (roots.library / "into").symlink_to(roots.downloads)
    # Albums holding a link (#898): the copy salmon uploads from, and the torrent, would take what it leads to.
    outside_track = roots.outside / "Album" / "01 Track 1.flac"
    _album(roots.downloads / "Linked track", tracks=1)
    (roots.downloads / "Linked track" / "02 Track 2.flac").symlink_to(outside_track)
    (roots.downloads / "Only a linked track").mkdir()
    (roots.downloads / "Only a linked track" / "01 Track 1.flac").symlink_to(outside_track)
    _album(roots.downloads / "Linked within", tracks=1)
    (roots.downloads / "Linked within" / "02 Track 2.flac").symlink_to("01 Track 1.flac")
    (roots.downloads / "Dangling").mkdir()
    (roots.downloads / "Dangling" / "01 Track 1.flac").symlink_to(roots.outside / "missing.flac")
    (roots.outside / "notes.txt").write_text("not the album's")
    _album(roots.downloads / "Linked log")
    (roots.downloads / "Linked log" / "rip.log").symlink_to(roots.outside / "notes.txt")
    _album(roots.downloads / "Linked folder")
    (roots.downloads / "Linked folder" / "Scans").symlink_to(roots.outside)
    _album(roots.downloads / "Linked in a disc folder" / "CD1")
    _album(roots.downloads / "Linked in a disc folder" / "CD2", tracks=0)
    (roots.downloads / "Linked in a disc folder" / "CD2" / "01 Track 1.flac").symlink_to(outside_track)
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
        "a folder of albums": (str(roots.downloads / "incoming"), 422),
        "a relative path": ("downloads/Album", 400),
        "a missing folder": (str(roots.downloads / "missing"), 404),
        "an audio file linked out of the roots": (str(roots.downloads / "Linked track"), 403),
        "only a linked audio file": (str(roots.downloads / "Only a linked track"), 403),
        "an audio file linked to one in the album": (str(roots.downloads / "Linked within"), 403),
        "a dangling audio link": (str(roots.downloads / "Dangling"), 403),
        "a linked log": (str(roots.downloads / "Linked log"), 403),
        "a linked folder": (str(roots.downloads / "Linked folder"), 403),
        "a link in a disc folder": (str(roots.downloads / "Linked in a disc folder"), 403),
    }
