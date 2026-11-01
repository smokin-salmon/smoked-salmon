"""Helpers of tests/test_uploader_dry_run.py that other test modules use too."""

import hashlib
import os
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
from aiohttp import web
from aiolimiter import AsyncLimiter
from asyncclick.testing import CliRunner
from humanfriendly import format_size
from mutagen.flac import FLAC
from torf import Torrent

import salmon.images
import salmon.images.imgbox
import salmon.tagger.foldername
import salmon.trackers
import salmon.uploader
import salmon.uploader.spectrals
from salmon import cfg
from salmon.config.validations import Seedbox
from salmon.trackers.base import BaseGazelleApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader.seedbox import UploadManager
from salmon.uploader.spectrals import create_specs_folder
from salmon.uploader.torrent_client import QBittorrentClient

RENAMED = "Artist - Album (2020) [WEB FLAC]"


AUTHKEY = "authkey-0123456789"


PASSKEY = "passkey-9876543210"


API_KEYS = {"RED": "red-api-key", "OPS": "ops-api-key"}


GROUP_ID = 55


FIRST_TORRENT_ID = 77


SOURCE_URL = "https://store.test/album/1"


# The run uploads to RED, then OPS: a new group on each (the empty answer), then no third tracker.
TWO_TRACKERS = "\nOPS\n\nn\n"


@dataclass
class Sent:
    """A request the fake tracker got. A file part of a form is (file name, content)."""

    method: str
    path: str
    query: dict[str, str]
    fields: list[tuple[str, Any]]


def _success(response: dict[str, Any]) -> web.Response:
    return web.json_response({"status": "success", "response": response})


def _group() -> dict[str, Any]:
    flac = {
        "id": 11,
        "media": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "remastered": True,
        "remasterYear": 2020,
        "remasterTitle": "",
        "remasterRecordLabel": "Label",
        "remasterCatalogueNumber": "CAT1",
    }
    group = {
        "id": GROUP_ID,
        "name": "Album",
        "year": 2020,
        "recordLabel": "Label",
        "catalogueNumber": "",
        "musicInfo": {"artists": [{"name": "Artist"}]},
    }
    return {"group": group, "torrents": [flac]}


class FakeTracker:
    """A local Gazelle tracker that answers what an upload asks for, and records every request it gets."""

    def __init__(self, group_torrents: list[dict[str, Any]] | None = None) -> None:
        """`group_torrents` replaces the torrents of the group torrentgroup answers (_group's WEB FLAC)."""
        self.sent: list[Sent] = []
        self.url = ""
        self._next_torrent_id = FIRST_TORRENT_ID
        self._group_torrents = group_torrents

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        fields: list[tuple[str, Any]] = []
        if request.method == "POST":
            for name, value in (await request.post()).items():
                is_file = isinstance(value, web.FileField)
                fields.append((name, (value.filename, value.file.read()) if is_file else value))
        self.sent.append(Sent(request.method, request.path, dict(request.query), fields))
        action = request.query.get("action")
        if request.path == "/ajax.php" and action == "index":
            return _success({"authkey": AUTHKEY, "passkey": PASSKEY})
        if request.path == "/ajax.php" and action in ("browse", "requests"):
            return _success({"results": []})
        if request.path == "/ajax.php" and action == "torrentgroup":
            group = _group()
            if self._group_torrents is not None:
                group["torrents"] = self._group_torrents
            return _success(group)
        if request.path == "/ajax.php" and action == "upload" and request.method == "POST":
            torrent_id, self._next_torrent_id = self._next_torrent_id, self._next_torrent_id + 1
            return _success({"torrentid": torrent_id, "groupid": GROUP_ID})
        if request.path == "/upload.php" and request.method == "POST":
            # The site's own form (RED takes a CD's logs there): the group page, the new torrent on it.
            torrent_id, self._next_torrent_id = self._next_torrent_id, self._next_torrent_id + 1
            return web.Response(
                text=f'<html><a class="tooltip" href="torrents.php?torrentid={torrent_id}">DL</a>'
                f'<a class="brackets" href="upload.php?groupid={GROUP_ID}">Add format</a></html>',
                content_type="text/html",
            )
        if request.path == "/reportsv2.php" and request.method == "POST":
            raise web.HTTPFound(f"/torrents.php?torrentid={dict(fields)['torrentid']}")
        if request.path == "/torrents.php":
            return web.Response(text="<html></html>")
        return web.Response(status=404)

    @asynccontextmanager
    async def serving(self) -> AsyncIterator["FakeTracker"]:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            yield self
        finally:
            await runner.cleanup()

    def not_gets(self) -> list[Sent]:
        return [sent for sent in self.sent if sent.method != "GET"]


def _client(cls: type[BaseGazelleApi], tracker: FakeTracker, torrents: Path) -> Any:
    """A real tracker client of this class, sending to the fake tracker."""
    site = cls()
    site.base_url = tracker.url
    site.api_key = API_KEYS[site.site_code]
    site.cookie = ""
    site.dot_torrents_dir = str(torrents)
    # Shadow the shared 5-per-10s limiter: the run sends a few dozen requests.
    site._rate_limiter = AsyncLimiter(1000, 1)
    return site


def _write_flac(path: Path, **tags: str) -> None:
    """Write a FLAC file with no audio: a STREAMINFO block and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()


def _album(folder: Path) -> Path:
    folder.mkdir(parents=True)
    # "year" is an alias standardize_tags rewrites to "date", in place.
    _write_flac(folder / "01 - one.flac", title="One", artist="Artist", year="2020")
    _write_flac(folder / "02 - two.flac", title="Two", artist="Artist", year="2020")
    (folder / "cover.jpg").write_bytes(b"jpeg")
    # Old mtimes, so a rewrite shows even within the filesystem's timestamp resolution.
    for entry in folder.rglob("*"):
        os.utime(entry, ns=(1_000_000_000, 1_000_000_000))
    return folder


def _snapshot(folder: Path) -> dict[str, tuple[str, int, int]]:
    """Every entry under folder: a hash of its bytes (or "dir"), its mtime and its inode, by relative path."""
    return {
        str(entry.relative_to(folder)): (
            hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else "dir",
            entry.stat().st_mtime_ns,
            entry.stat().st_ino,
        )
        for entry in sorted(folder.rglob("*"))
    }


def _returning(result: Any = None):
    def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _returning_async(result: Any = None):
    async def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


async def _retag(path: str, *_args: Any, **_kwargs: Any) -> None:
    """Stands in for tag_files: rewrites a tag in every FLAC of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            tagged = FLAC(os.path.join(path, name))
            tagged["album"] = "Album"
            tagged.save()


async def _rename_files(path: str, *_args: Any, **_kwargs: Any) -> None:
    """Stands in for rename_files: renames the tracks of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            os.rename(os.path.join(path, name), os.path.join(path, name.replace(" - ", ". ")))


async def _lossy_with_spectrals(path: str, *_args: Any, **_kwargs: Any) -> tuple[bool, dict[int, str]]:
    """Stands in for check_spectrals: the release is a lossy master, and track 1's spectrals go in."""
    spectrals = Path(create_specs_folder(path))
    for name in ("01 Full.png", "01 Zoom.png"):
        (spectrals / name).write_bytes(b"png")
    return True, {1: "01. one.flac"}


async def _transcode(path: str, bitrate: str, *_args: Any, output_dir: str | None = None, **_kw: Any) -> str:
    """Stands in for transcode_folder: writes MP3s with made-up content."""
    new_path = Path(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bitrate}]")
    new_path.mkdir(exist_ok=True)
    for name in ("01. one.mp3", "02. two.mp3"):
        (new_path / name).write_bytes(bitrate.encode() * 1000)
    return str(new_path)


def _track(number: int, title: str) -> dict[str, Any]:
    tags = SimpleNamespace(tracknumber=str(number), discnumber=None, artist=["Artist"], title=title)
    return {"sample rate": 44100, "precision": 16, "duration": 60, "bit rate": 1_000_000, "t": tags}


@dataclass
class Run:
    result: Any
    tracker: FakeTracker
    queued: list[tuple[Any, ...]]
    logins: list[str | None]


def _run_up(
    monkeypatch,
    album: Path,
    torrents: Path,
    args: tuple[str, ...] = (),
    input: str = TWO_TRACKERS,
    *,
    classes: dict[str, type[BaseGazelleApi]] | None = None,
    multi_tracker_upload: bool = True,
    yes_all: bool = True,
    trackers: tuple[str, ...] | None = None,
    **fakes: Any,
) -> Run:
    """Run `salmon up ALBUM` against the fake tracker: real staging, torrents and forms.

    Without `trackers`, no -t and tracker.default_trackers = ["RED"]: RED, then the other trackers are offered.
    `trackers` are named with -t, each; `()` names none and leaves default_trackers as the test set it.
    The seams that need audio tools, a metadata source or a reviewer are stubbed, `fakes` replace more of them.
    `classes` gives the client class of each site code, RED and OPS by default. `yes_all=False` leaves out -yyy.
    """
    tracker, queued, logins = fake_upload_world(
        monkeypatch, torrents, classes=classes, multi_tracker_upload=multi_tracker_upload, **fakes
    )
    if trackers is None:
        monkeypatch.setattr(cfg.tracker, "default_trackers", ["RED"])
        trackers = ()

    async def run():
        async with tracker.serving():
            return await CliRunner().invoke(
                salmon.uploader.up,
                [str(album), *(arg for code in trackers for arg in ("-t", code)), "-s", "WEB", "-n"]
                + (["-yyy"] if yes_all else [])
                + ["--skip-integrity-check", "--skip-up"]
                + ["--source-url", SOURCE_URL, *args],
                input=input,
            )

    return Run(anyio.run(run), tracker, queued, logins)


def fake_upload_world(
    monkeypatch,
    torrents: Path,
    *,
    classes: dict[str, type[BaseGazelleApi]] | None = None,
    multi_tracker_upload: bool = True,
    tracker: "FakeTracker | None" = None,
    **fakes: Any,
) -> tuple[FakeTracker, list[tuple[Any, ...]], list[str | None]]:
    """Stub what an upload needs besides the tracker, and send its tracker requests to `tracker`, else a new fake.

    Returns:
        The fake tracker (not serving yet), the seedbox tasks the run queues, and the torrent client logins.
    """
    rls_data = {
        "format": "FLAC",
        "encoding": "Lossless",
        "artists": [("Artist", "main")],
        "title": "Album",
        "catno": "CAT1",
    }
    metadata = {
        **rls_data,
        "source": "WEB",
        "year": 2020,
        "group_year": 2020,
        "date": "2020-01-01",
        "label": "Label",
        "rls_type": "Album",
        "edition_title": None,
        "encoding_vbr": False,
        "scene": False,
        "cover": None,
        "comment": None,
        "urls": [],
        "genres": ["Electronic"],
    }
    track_data = {"01. one.flac": _track(1, "One"), "02. two.flac": _track(2, "Two")}
    for name, fake in {
        "gather_audio_info": _returning({}),
        "check_hybrid": _returning(False),
        "gather_tags": _returning({}),
        "construct_rls_data": _returning_async(rls_data),
        "mqa_test": _returning_async(),
        "check_spectrals": _lossy_with_spectrals,
        "get_metadata": _returning_async((metadata, None)),
        "review_metadata_with_ai": _returning_async(metadata),
        "tag_files": _retag,
        "check_tags": _returning_async({}),
        "rename_files": _rename_files,
        "check_folder_structure": _returning_async(),
        "concat_track_data": _returning(track_data),
        "transcode_folder": _transcode,
        **fakes,
    }.items():
        monkeypatch.setattr(salmon.uploader, name, fake)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    # A config with yes_all on would answer the questions the run is meant to ask.
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", multi_tracker_upload)
    monkeypatch.setattr(cfg.upload, "upload_to_seedbox", True)
    monkeypatch.setattr(cfg.upload.requests, "check_requests", True)
    monkeypatch.setattr(cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(cfg.image, "embed_missing_cover", False)
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(UploadManager, "add_upload_task", lambda _self, *task, **_kw: queued.append(task))
    # A seedbox whose torrent client is logged into when the upload manager is set up.
    monkeypatch.setattr(cfg, "seedbox", [Seedbox(name="box", torrent_client="qbittorrent+http://127.0.0.1:9")])
    logins: list[str | None] = []
    monkeypatch.setattr(QBittorrentClient, "login", lambda client: logins.append(client.url))
    tracker = tracker or FakeTracker()
    classes = classes or {"RED": RedApi, "OPS": OpsApi}
    monkeypatch.setattr(salmon.trackers, "get_class", lambda code: lambda: _client(classes[code], tracker, torrents))
    return tracker, queued, logins


def _printed_uploads(output: str) -> list[tuple[str, list[tuple[str, str]], list[str]]]:
    """Each upload a dry run printed: the line saying where it would go, its form's fields, and the torrent's lines."""
    uploads: list[tuple[str, list[tuple[str, str]], list[str]]] = []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("Dry run: not uploading to "):
            continue
        fields: list[tuple[str, str]] = []
        torrent: list[str] = []
        for part in lines[i + 1 :]:
            if part.startswith("  The torrent: ") or (torrent and part.startswith("    ")):
                torrent.append(part)
            elif torrent or not part.startswith("  "):
                break
            elif part.startswith("      "):
                name, value = fields[-1]
                fields[-1] = (name, f"{value}\n{part[6:]}")
            else:
                name, _, value = part[2:].partition(": ")
                fields.append((name, value))
        uploads.append((line, fields, torrent))
    return uploads


def _torrent_lines(torrent_data: bytes) -> list[str]:
    torrent = Torrent.read_stream(torrent_data)
    lines = [
        f"  The torrent: {torrent.name}, {len(torrent.files)} file(s), {format_size(torrent.size, binary=True)}, "
        f"piece size {format_size(torrent.piece_size, binary=True)}, source {torrent.source}"
    ]
    files = [f"    {'/'.join(file.parts[1:])}  ({format_size(file.size, binary=True)})" for file in torrent.files]
    return lines + files
