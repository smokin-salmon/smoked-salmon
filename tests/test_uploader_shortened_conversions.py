"""A conversion whose file names the path check shortened is salmon's own: the next tracker's turn reuses it.

In a multi-tracker run, the first tracker's turn makes a transcode or a downconversion and the folder check shortens
its long file names. The second tracker's turn used to recognise the conversion only by the names it would write, so
it deleted the transcode the first torrent seeds from and made it again, and refused the downconversion.

The runs go against the local fake tracker of test_uploader_dry_run, never a real one. transcode_folder and
convert_folder are the real ones (their output names, their reuse of an existing folder), with only the MP3 encoding
and sox faked; so is the folder check.
"""

import os
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from mutagen.flac import FLAC
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    RENAMED,
    TWO_TRACKERS,
    FakeTracker,
    _returning,
    _returning_async,
    _run_up,
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)
from torf import Torrent

import salmon.converter.downconverting
import salmon.converter.transcoding
from salmon import cfg
from salmon.converter.downconverting import convert_folder
from salmon.converter.transcoding import transcode_folder
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.folderstructure import check_folder_structure

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

# "RENAMED/01. <title>.flac" is 180 characters: the album fits RED's limit, the strictest of the run's. Its
# conversions' folders are a little longer ("[WEB MP3 V0]", "[WEB 16bit FLAC]"), so their first track is shortened.
LONG_TITLE = "x" * (180 - len(f"{RENAMED}/01. .flac"))
MP3_320 = "Artist - Album (2020) [WEB MP3 320]"
MP3_V0 = "Artist - Album (2020) [WEB MP3 V0]"
FLAC_16 = "Artist - Album (2020) [WEB 16bit FLAC]"


@dataclass
class World:
    """What the runs of a test did: each MP3 or sox conversion made, each folder removed, each file it checked."""

    tmp_path: Path
    encoded: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # Each conversion folder's audio files after each check: (folder name, {relative path: inode}).
    checked: list[tuple[str, dict[str, int]]] = field(default_factory=list)
    # Held open, so an inode cannot be handed to a new file while the test still compares it.
    handles: list[Any] = field(default_factory=list)


def _write_flac(path: Path, rate: int, bits: int, **tags: str) -> None:
    """Write a FLAC file with no audio: a STREAMINFO block at this rate and depth, and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((rate << 44) | (1 << 41) | ((bits - 1) << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Run the real converters, encoding nothing, and the real folder check; note every folder removed."""
    world = World(tmp_path)

    async def encode(items: list[Any], bitrate: str) -> None:
        for item in items:
            world.encoded.append(os.path.basename(item.dst))
            Path(item.dst).write_bytes(bitrate.encode() * 1000)

    async def sox(items: list[Any], bit_depth: int) -> None:
        for item in items:
            world.encoded.append(os.path.basename(item.dst))
            Path(item.dst).parent.mkdir(parents=True, exist_ok=True)
            _write_flac(Path(item.dst), item.target_rate, bit_depth)

    real_rmtree = shutil.rmtree

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        world.removed.append(os.path.basename(os.fspath(path)))
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(salmon.converter.transcoding, "_transcode_audio_files", encode)
    monkeypatch.setattr(salmon.converter.downconverting, "_convert_audio_files", sox)
    monkeypatch.setattr(salmon.converter.transcoding.shutil, "rmtree", rmtree)
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    yield world
    for handle in world.handles:
        handle.close()


def _album(folder: Path, rate: int, bits: int, title: str) -> Path:
    """An album whose first track is named after title; the run renames it to "01. <title>.flac"."""
    folder.mkdir(parents=True)
    _write_flac(folder / f"01 - {title}.flac", rate, bits, title="One", artist="Artist", year="2020")
    _write_flac(folder / "02 - two.flac", rate, bits, title="Two", artist="Artist", year="2020")
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


def _up(
    monkeypatch, world: World, name: str, title: str = LONG_TITLE, rate: int = 44100, bits: int = 16, **kwargs: Any
):
    """Run salmon up -t RED, then OPS when asked (multi_tracker_upload), from and into directories of its own."""
    base = world.tmp_path / name
    downloads, torrents = base / "downloads", base / "torrents"
    downloads.mkdir(parents=True)
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))

    async def check(path: str, scene: bool, **check_kwargs: Any) -> None:
        await check_folder_structure(path, scene, **check_kwargs)
        if os.path.basename(path) == RENAMED:
            return
        inodes = {}
        for root, _dirs, files in os.walk(path):
            for file in files:
                if file.endswith((".mp3", ".flac")):
                    full = os.path.join(root, file)
                    world.handles.append(open(full, "rb"))  # noqa: SIM115 (closed by the fixture)
                    inodes[os.path.relpath(full, path)] = os.stat(full).st_ino
        world.checked.append((os.path.basename(path), inodes))

    run = _run_up(
        monkeypatch,
        _album(base / "seeding" / "Album", rate, bits, title),
        torrents,
        input=TWO_TRACKERS,
        transcode_folder=transcode_folder,
        convert_folder=convert_folder,
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


def _requests(run) -> list[tuple[str, str, str | None]]:
    return [(sent.method, sent.path, sent.query.get("action")) for sent in run.tracker.sent]


def _audio(paths: list[str]) -> list[str]:
    return sorted(path for path in paths if path.endswith((".mp3", ".flac")))


def test_the_second_tracker_reuses_the_shortened_transcodes(monkeypatch, world) -> None:
    run = _up(monkeypatch, world, "run")

    # Made once, on RED's turn, and never removed: OPS's turn takes the same files.
    assert sorted(world.encoded) == sorted([f"01. {LONG_TITLE}.mp3", "02. two.mp3"] * 2)
    assert not {MP3_320, MP3_V0} & set(world.removed)
    assert "re-transcoding" not in run.result.output
    assert f"{MP3_320} already exists." in run.result.output
    assert f"{MP3_V0} already exists." in run.result.output
    # Each folder's files, at the check of each turn: the same names and the same inodes.
    assert [folder for folder, _inodes in world.checked] == [MP3_320, MP3_V0, MP3_320, MP3_V0]
    assert world.checked[0] == world.checked[2]
    assert world.checked[1] == world.checked[3]
    assert f"01. {LONG_TITLE[:-4]}...mp3" in world.checked[0][1]

    uploads = _uploads(run)
    assert [(tracker, folder) for tracker, folder, _paths in uploads] == [
        ("RED", RENAMED),
        ("RED", MP3_320),
        ("RED", MP3_V0),
        ("OPS", RENAMED),
        ("OPS", MP3_320),
        ("OPS", MP3_V0),
    ]
    assert uploads[1][2] == uploads[4][2]
    assert uploads[2][2] == uploads[5][2]


def _24bit_run(monkeypatch, world: World, name: str, title: str = LONG_TITLE):
    """A 24-bit 96 kHz album, whose run makes its 16-bit 48 kHz downconversion only."""
    rls_data = {
        "format": "FLAC",
        "encoding": "24bit Lossless",
        "artists": [("Artist", "main")],
        "title": "Album",
        "catno": "CAT1",
    }

    def track(number: int, title: str) -> dict[str, Any]:
        tags = type("Tags", (), {"tracknumber": str(number), "discnumber": None, "artist": ["Artist"], "title": title})
        return {"sample rate": 96000, "precision": 24, "duration": 60, "bit rate": 1_000_000, "t": tags}

    track_data = {f"01. {title}.flac": track(1, "One"), "02. two.flac": track(2, "Two")}
    return _up(
        monkeypatch,
        world,
        name,
        title,
        rate=96000,
        bits=24,
        args=("--conversions", "16bit"),
        construct_rls_data=_returning_async(rls_data),
        concat_track_data=_returning(track_data),
        gather_audio_info=gather_audio_info,
        # The group holds no 16-bit FLAC yet: -yyy makes the downconversion.
        tracker=FakeTracker(group_torrents=[]),
    )


def test_the_second_tracker_gets_the_shortened_downconversion(monkeypatch, world) -> None:
    run = _24bit_run(monkeypatch, world, "run")

    # Made once, on RED's turn; OPS's turn takes the same files.
    assert sorted(world.encoded) == sorted([f"01. {LONG_TITLE}.flac", "02. two.flac"])
    assert "is incomplete or not salmon's conversion" not in run.result.output
    assert "not uploading it" not in run.result.output
    assert f"{FLAC_16} already exists." in run.result.output
    assert [folder for folder, _inodes in world.checked] == [FLAC_16, FLAC_16]
    assert world.checked[0] == world.checked[1]
    assert f"01. {LONG_TITLE[:-8]}...flac" in world.checked[0][1]

    uploads = _uploads(run)
    assert [(tracker, folder) for tracker, folder, _paths in uploads] == [
        ("RED", RENAMED),
        ("RED", FLAC_16),
        ("OPS", RENAMED),
        ("OPS", FLAC_16),
    ]
    assert _audio(uploads[1][2]) == _audio(uploads[3][2])


# What the transcode run sends on master, where OPS's turn deletes and remakes the transcodes: the same requests.
MASTER_REQUESTS = [
    *[
        ("GET", "/ajax.php", "index"),
        ("GET", "/ajax.php", "browse"),
        ("GET", "/ajax.php", "requests"),
        ("POST", "/ajax.php", "upload"),
        ("POST", "/reportsv2.php", "takereport"),
        ("GET", "/torrents.php", None),
        ("GET", "/ajax.php", "torrentgroup"),
        *[("POST", "/ajax.php", "upload"), ("POST", "/reportsv2.php", "takereport"), ("GET", "/torrents.php", None)]
        * 2,
    ]
    * 2,
]


def test_reusing_shortened_transcodes_sends_what_master_sent(monkeypatch, world) -> None:
    """The fix only changes what happens on disk: the tracker gets what it got before, request for request."""
    run = _up(monkeypatch, world, "long")
    short_names = _up(monkeypatch, world, "short", title="one")

    assert _requests(run) == MASTER_REQUESTS
    assert _requests(short_names) == MASTER_REQUESTS


def test_a_shortened_downconversion_sends_what_an_unshortened_one_does(monkeypatch, world) -> None:
    run = _24bit_run(monkeypatch, world, "long")
    short_names = _24bit_run(monkeypatch, world, "short", title="one")

    assert _requests(run) == _requests(short_names)
