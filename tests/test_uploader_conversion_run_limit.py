"""salmon up checks the conversions it makes against the run's path limit, the one its album folder was checked
against: with -t OPS -t RED, a transcode made during OPS's turn is truncated to RED's 180 characters too.

The runs go against the local fake tracker of helpers.uploader_dry_run, never a real one. transcode_folder is the real
one (its output names, its reuse of an existing folder), with only the MP3 encoding faked; so is the folder check.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from helpers.uploader_dry_run import (
    RENAMED,
    _run_up,
    _write_flac,
)
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)
from torf import Torrent

import salmon.converter.transcoding
from salmon import cfg
from salmon.converter.transcoding import transcode_folder
from salmon.tagger.folderstructure import check_folder_structure

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

# "RENAMED/01. <title>.flac" is 180 characters: the album fits RED's limit. Its transcodes' folders are a little
# longer ("[WEB MP3 V0]", "[WEB MP3 320]"), so their paths are 181 and 182: within OPS's 255, not RED's 180.
LONG_TITLE = "x" * (180 - len(f"{RENAMED}/01. .flac"))
MP3_320 = "Artist - Album (2020) [WEB MP3 320]"
MP3_V0 = "Artist - Album (2020) [WEB MP3 V0]"


@dataclass
class World:
    """Where the runs of a test put their files, and each folder check they made: (folder name, limit)."""

    tmp_path: Path
    checks: list[tuple[str, int]] = field(default_factory=list)


@pytest.fixture
def world(monkeypatch, tmp_path) -> World:
    """Run the real transcode_folder, encoding nothing, and the real folder check."""

    async def encode(items: list[Any], bitrate: str) -> None:
        for item in items:
            Path(item.dst).write_bytes(bitrate.encode() * 1000)

    monkeypatch.setattr(salmon.converter.transcoding, "_transcode_audio_files", encode)
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return World(tmp_path)


def _album(folder: Path, title: str) -> Path:
    """An album whose first track is named after title; the run renames it to "01. <title>.flac"."""
    folder.mkdir(parents=True)
    _write_flac(folder / f"01 - {title}.flac", title="One", artist="Artist", year="2020")
    _write_flac(folder / "02 - two.flac", title="Two", artist="Artist", year="2020")
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


def _up(monkeypatch, world: World, name: str, trackers: tuple[str, ...], title: str = LONG_TITLE, **kwargs: Any):
    """Run salmon up -t each of trackers, from and into directories of its own under name."""
    base = world.tmp_path / name
    downloads, torrents = base / "downloads", base / "torrents"
    downloads.mkdir(parents=True)
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))

    async def check(path: str, scene: bool, **check_kwargs: Any) -> None:
        world.checks.append((os.path.basename(path), check_kwargs["max_path_length"]))
        await check_folder_structure(path, scene, **check_kwargs)

    run = _run_up(
        monkeypatch,
        _album(base / "seeding" / "Album", title),
        torrents,
        trackers=trackers,
        transcode_folder=transcode_folder,
        check_folder_structure=check,
        **kwargs,
    )
    assert run.result.exit_code == 0, run.result.output
    assert "Traceback" not in run.result.output
    return run


def _uploads(run) -> list[tuple[str, str, list[str]]]:
    """Each torrent uploaded: the tracker it is for, its folder, and each file's in-torrent path."""
    uploads = []
    for sent in run.tracker.sent:
        if sent.query.get("action") == "upload":
            torrent = Torrent.read_stream(dict(sent.fields)["file_input"][1])
            uploads.append((str(torrent.source), str(torrent.name), ["/".join(file.parts) for file in torrent.files]))
    return uploads


def _longest(run) -> list[tuple[str, str, int]]:
    """Each torrent uploaded: the tracker it is for, its folder, and its longest in-torrent path."""
    return [(tracker, name, max(len(path) for path in paths)) for tracker, name, paths in _uploads(run)]


def _requests(run) -> list[tuple[str, str, str | None]]:
    return [(sent.method, sent.path, sent.query.get("action")) for sent in run.tracker.sent]


def test_ops_then_red_truncates_ops_transcodes_to_reds_limit(monkeypatch, world) -> None:
    run = _up(monkeypatch, world, "run", ("OPS", "RED"), multi_tracker_upload=False)

    # The album and every conversion, on both trackers' turns, are checked against RED's 180.
    assert {limit for _folder, limit in world.checks} == {180}
    assert _longest(run) == [
        ("OPS", RENAMED, 180),
        ("OPS", MP3_320, 180),
        ("OPS", MP3_V0, 180),
        ("RED", RENAMED, 180),
        ("RED", MP3_320, 180),
        ("RED", MP3_V0, 180),
    ]
    # Shortened as the album folder's own files would be: to 180 characters, the ".." marker included.
    uploads = _uploads(run)
    assert f"{MP3_320}/01. {LONG_TITLE[:-4]}...mp3" in uploads[1][2]
    assert f"{MP3_V0}/01. {LONG_TITLE[:-3]}...mp3" in uploads[2][2]
    # Both trackers got the same files.
    assert uploads[1][2] == uploads[4][2]
    assert uploads[2][2] == uploads[5][2]


def test_the_shorter_limit_sends_no_other_request(monkeypatch, world) -> None:
    short_names = _up(monkeypatch, world, "short", ("OPS", "RED"), title="one", multi_tracker_upload=False)

    run = _up(monkeypatch, world, "long", ("OPS", "RED"), multi_tracker_upload=False)

    assert _requests(run) == _requests(short_names)


def test_ops_alone_keeps_its_own_limit(monkeypatch, world) -> None:
    run = _up(monkeypatch, world, "run", ("OPS",), multi_tracker_upload=False)

    assert {limit for _folder, limit in world.checks} == {255}
    assert _longest(run) == [("OPS", RENAMED, 180), ("OPS", MP3_320, 182), ("OPS", MP3_V0, 181)]
