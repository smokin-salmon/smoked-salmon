"""salmon up --dry-run: the whole upload is built, and nothing is sent (#532).

The runs here go against a local fake tracker and a fake image host, never a real one.
"""

import hashlib
import os
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import anyio
import pytest
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
from salmon import cfg, dryrun
from salmon.common.redaction import redact_tracker_text
from salmon.config.validations import Seedbox
from salmon.errors import DryRunRefused
from salmon.images.base import BaseImageUploader
from salmon.trackers.base import BaseGazelleApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader import staging
from salmon.uploader.seedbox import UploadManager
from salmon.uploader.spectrals import get_spectrals_path
from salmon.uploader.torrent_client import QBittorrentClient

RENAMED = "Artist - Album (2020) [WEB FLAC]"
AUTHKEY = "authkey-0123456789"
PASSKEY = "passkey-9876543210"
API_KEYS = {"RED": "red-api-key", "OPS": "ops-api-key"}
GROUP_ID = 55
FIRST_TORRENT_ID = 77
SOURCE_URL = "https://store.test/album/1"
NEW_GROUP = str(dryrun.NEW_GROUP_ID)
NEW_TORRENT = str(dryrun.NEW_TORRENT_ID)
# The run uploads to RED, then OPS: a new group on each (the empty answer), then no third tracker.
TWO_TRACKERS = "\nOPS\n\nn\n"


# A fake tracker


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

    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.url = ""
        self._next_torrent_id = FIRST_TORRENT_ID

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
            return _success(_group())
        if request.path == "/ajax.php" and action == "upload" and request.method == "POST":
            torrent_id, self._next_torrent_id = self._next_torrent_id, self._next_torrent_id + 1
            return _success({"torrentid": torrent_id, "groupid": GROUP_ID})
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


# A fake image host


@pytest.fixture
def image_uploads(monkeypatch) -> list[tuple[str, str]]:
    """Make "testhost" the cover and spectrals host, and give the (file name, URL) of each image it takes."""
    uploads: list[tuple[str, str]] = []

    class ImageUploader(BaseImageUploader):
        async def upload_file(self, filename: str) -> tuple[str, None]:
            url = f"https://images.test/{len(uploads) + 1}.png"
            uploads.append((os.path.basename(filename), url))
            return url, None

    monkeypatch.setitem(salmon.images.HOSTS, "testhost", SimpleNamespace(ImageUploader=ImageUploader))
    monkeypatch.setattr(cfg.image, "cover_uploader", "testhost")
    monkeypatch.setattr(cfg.image, "specs_uploader", "testhost")
    return uploads


# An album and a run of salmon up


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


def _retag(path: str, *_args: Any) -> None:
    """Stands in for tag_files: rewrites a tag in every FLAC of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            tagged = FLAC(os.path.join(path, name))
            tagged["album"] = "Album"
            tagged.save()


def _rename_files(path: str, *_args: Any) -> None:
    """Stands in for rename_files: renames the tracks of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            os.rename(os.path.join(path, name), os.path.join(path, name.replace(" - ", ". ")))


async def _lossy_with_spectrals(path: str, *_args: Any, **_kwargs: Any) -> tuple[bool, dict[int, str]]:
    """Stands in for check_spectrals: the release is a lossy master, and track 1's spectrals go in."""
    spectrals = Path(get_spectrals_path(path))
    spectrals.mkdir()
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
    monkeypatch, album: Path, torrents: Path, args: tuple[str, ...] = (), input: str = TWO_TRACKERS, **fakes: Any
) -> Run:
    """Run `salmon up ALBUM -t RED` against the fake tracker, with the real staging, torrents and upload forms.

    The seams that need audio tools, a metadata source or a reviewer are stubbed, `fakes` replace more of them.
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
        "construct_rls_data": _returning(rls_data),
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
    # -yyy sets yes_all: patched, so it is put back after the test.
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", True)
    monkeypatch.setattr(cfg.upload, "upload_to_seedbox", True)
    monkeypatch.setattr(cfg.upload.requests, "check_requests", True)
    monkeypatch.setattr(cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(cfg.image, "auto_compress_cover", False)
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(UploadManager, "add_upload_task", lambda _self, *task, **_kw: queued.append(task))
    # A seedbox whose torrent client is logged into when the upload manager is set up.
    monkeypatch.setattr(cfg, "seedbox", [Seedbox(name="box", torrent_client="qbittorrent+http://127.0.0.1:9")])
    logins: list[str | None] = []
    monkeypatch.setattr(QBittorrentClient, "login", lambda client: logins.append(client.url))
    tracker = FakeTracker()
    classes = {"RED": RedApi, "OPS": OpsApi}
    monkeypatch.setattr(salmon.trackers, "get_class", lambda code: lambda: _client(classes[code], tracker, torrents))

    async def run():
        async with tracker.serving():
            return await CliRunner().invoke(
                salmon.uploader.up,
                [str(album), "-t", "RED", "-s", "WEB", "-n", "-yyy", "--skip-integrity-check", "--skip-up"]
                + ["--source-url", SOURCE_URL, *args],
                input=input,
            )

    return Run(anyio.run(run), tracker, queued, logins)


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path, Path]:
    """A library, a download_directory and a dot_torrents_dir, configured."""
    library = tmp_path / "library"
    downloads = tmp_path / "downloads"
    torrents = tmp_path / "torrents"
    for folder in (library, downloads, torrents):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    return library, downloads, torrents


# What a dry run printed


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


def _printed_reports(output: str) -> list[str]:
    """The text of each lossy master report a dry run printed instead of sending."""
    reports: list[str] = []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if "for lossy master approval. The report:" in line:
            text: list[str] = []
            for part in lines[i + 1 :]:
                if part and not part.startswith("  "):
                    break
                text.append(part[2:])
            reports.append("\n".join(text).strip("\n"))
    return reports


def _torrent_lines(torrent_data: bytes) -> list[str]:
    torrent = Torrent.read_stream(torrent_data)
    lines = [
        f"  The torrent: {torrent.name}, {len(torrent.files)} file(s), {format_size(torrent.size, binary=True)}, "
        f"piece size {format_size(torrent.piece_size, binary=True)}, source {torrent.source}"
    ]
    files = [f"    {'/'.join(file.parts[1:])}  ({format_size(file.size, binary=True)})" for file in torrent.files]
    return lines + files


# The dry run


@pytest.mark.parametrize("where", ["elsewhere", "in library_dirs"])
def test_a_dry_run_sends_nothing_and_leaves_nothing_behind(monkeypatch, dirs, image_uploads, where: str) -> None:
    library, downloads, torrents = dirs
    album = _album((library / "Artist" if where == "in library_dirs" else downloads.parent / "seeding") / "Album")
    before = _snapshot(album)

    run = _run_up(monkeypatch, album, torrents, args=("--dry-run",))

    assert run.result.exit_code == 0, run.result.output
    # Only reads: the login check, the group search and the request search, on both trackers.
    assert run.tracker.not_gets() == []
    assert {(sent.path, sent.query["action"]) for sent in run.tracker.sent} == {
        ("/ajax.php", "index"),
        ("/ajax.php", "browse"),
        ("/ajax.php", "requests"),
    }
    assert image_uploads == []
    assert run.queued == []
    assert run.logins == []
    assert "Dry run: not connecting to the seedboxes' torrent clients." in run.result.output
    assert _snapshot(album) == before
    # No torrent file where a client could pick it up, and nothing left in download_directory: the scratch copy,
    # its torrents and its transcodes went with its run directory.
    assert os.listdir(torrents) == []
    assert os.listdir(downloads) == [staging.STAGING_DIR]
    assert os.listdir(downloads / staging.STAGING_DIR) == []
    # What each upload would send was printed: the FLAC and both transcodes, to each tracker.
    uploads = _printed_uploads(run.result.output)
    assert [line.split(".")[0] for line, _fields, _torrent in uploads] == ["Dry run: not uploading to RED"] * 3 + [
        "Dry run: not uploading to OPS"
    ] * 3
    assert [dict(fields)["format"] + " " + dict(fields)["bitrate"] for _line, fields, _torrent in uploads[:3]] == [
        "FLAC Lossless",
        "MP3 320",
        "MP3 V0 (VBR)",
    ]
    # The lossy master reports were printed instead of sent, one per torrent.
    assert len(_printed_reports(run.result.output)) == 6
    assert "Dry run: not uploading the cover cover.jpg to testhost." in run.result.output
    assert "Dry run: not uploading the spectrals of 1 track(s) to testhost." in run.result.output
    assert f"Dry run: done. Nothing was sent, and {album} is unchanged." in run.result.output


def test_a_dry_run_prints_the_forms_the_real_run_sends(monkeypatch, tmp_path, dirs, image_uploads) -> None:
    _library, _downloads, torrents = dirs
    real = _run_up(monkeypatch, _album(tmp_path / "real" / "Album"), torrents)
    assert real.result.exit_code == 0, real.result.output
    # The real run logs into the torrent client and queues its seedbox tasks; the dry run does neither.
    assert real.logins and real.queued
    real_images = list(image_uploads)
    image_uploads.clear()

    dry = _run_up(monkeypatch, _album(tmp_path / "dry" / "Album"), torrents, args=("--dry-run",))
    assert dry.result.exit_code == 0, dry.result.output
    assert dry.logins == dry.queued == []

    def as_printed(value: str) -> str:
        """A value the real run sent, as the dry run shows it: redacted, with what only a real upload gets
        (image URLs, the new group's and torrents' IDs) replaced by what stands in for it."""
        for name, url in real_images:
            value = value.replace(url, dryrun.image_url(name, "testhost"))
        for torrent_id in range(FIRST_TORRENT_ID, FIRST_TORRENT_ID + 6):
            value = value.replace(f"torrentid={torrent_id}", f"torrentid={NEW_TORRENT}")
        value = value.replace(real.tracker.url, dry.tracker.url)
        return redact_tracker_text(value, [AUTHKEY, PASSKEY, *API_KEYS.values()])

    real_uploads = [sent for sent in real.tracker.sent if sent.query.get("action") == "upload"]
    printed = _printed_uploads(dry.result.output)
    assert len(real_uploads) == len(printed) == 6
    for sent, (line, fields, torrent) in zip(real_uploads, printed, strict=True):
        assert line.endswith(f"It would send POST {dry.tracker.url}{sent.path}?action=upload with the API key:")
        expected = [
            (name, f"{value[0]} ({format_size(len(value[1]), binary=True)})")
            if isinstance(value, tuple)
            else (name, NEW_GROUP if name == "groupid" and value == str(GROUP_ID) else as_printed(value))
            for name, value in sent.fields
        ]
        assert fields == expected
        assert torrent == _torrent_lines(dict(sent.fields)["file_input"][1])
    assert ("auth", "[REDACTED]") in printed[0][1]
    # The transcodes go into the group the FLAC upload creates, and link to that upload.
    assert ("groupid", NEW_GROUP) in printed[1][1]
    assert f"torrentid={NEW_TORRENT}" in dict(printed[1][1])["release_desc"]

    real_reports = [dict(sent.fields)["extra"] for sent in real.tracker.sent if sent.path == "/reportsv2.php"]
    assert len(real_reports) == 6
    assert _printed_reports(dry.result.output) == [as_printed(report).strip("\n") for report in real_reports]


def test_a_dry_run_with_skip_flac_upload_prints_the_transcodes_into_the_group(
    monkeypatch, tmp_path, dirs, image_uploads
) -> None:
    _library, downloads, torrents = dirs
    album = _album(tmp_path / "seeding" / "Album")
    before = _snapshot(album)

    run = _run_up(
        monkeypatch,
        album,
        torrents,
        args=("--dry-run", "-g", str(GROUP_ID), "--skip-flac-upload"),
        input="y\n",
        check_spectrals=_returning_async((False, None)),
    )

    assert run.result.exit_code == 0, run.result.output
    assert run.tracker.not_gets() == []
    assert image_uploads == []
    assert _snapshot(album) == before
    assert os.listdir(torrents) == []
    assert os.listdir(downloads / staging.STAGING_DIR) == []
    uploads = _printed_uploads(run.result.output)
    assert [dict(fields)["bitrate"] for _line, fields, _torrent in uploads] == ["320", "V0 (VBR)"]
    assert all(("groupid", str(GROUP_ID)) in fields for _line, fields, _torrent in uploads)
    assert all("torrentid=11" in dict(fields)["release_desc"] for _line, fields, _torrent in uploads)


def test_a_dry_run_cannot_check_spectrals_after_the_upload(monkeypatch, dirs, image_uploads) -> None:
    library, _downloads, torrents = dirs
    run = _run_up(monkeypatch, _album(library / "Album"), torrents, args=("--dry-run", "--spectrals-after"))

    assert run.result.exit_code == 2
    assert "--dry-run cannot be used with --spectrals-after" in run.result.output
    assert run.tracker.sent == []


# The guard: a step that sends something and skips nothing in a dry run is stopped before it sends.


def test_a_tracker_post_with_no_skip_stops_the_dry_run_before_it_is_sent(monkeypatch, dirs, image_uploads) -> None:
    library, downloads, torrents = dirs
    album = _album(library / "Album")
    before = _snapshot(album)

    async def report_with_no_skip(gazelle_site: BaseGazelleApi, torrent_id: int, *_args: Any, **_kw: Any) -> None:
        # A step someone added without a dry run skip.
        await gazelle_site.report_lossy_master(torrent_id, "comment", "WEB")

    run = _run_up(monkeypatch, album, torrents, args=("--dry-run",), report_lossy_master=report_with_no_skip)

    assert run.result.exit_code == 1
    assert "Dry run stopped before it could send POST" in run.result.output
    assert "reportsv2.php" in run.result.output
    assert run.tracker.not_gets() == []
    assert _snapshot(album) == before
    assert os.listdir(downloads / staging.STAGING_DIR) == []
    assert os.listdir(torrents) == []


def test_an_image_upload_with_no_skip_stops_the_dry_run_before_it_is_sent(monkeypatch, dirs, image_uploads) -> None:
    library, downloads, torrents = dirs
    album = _album(library / "Album")

    async def spectrals_with_no_skip(spectrals_path: str, spectral_ids: dict[int, str]) -> Any:
        # A step someone added without a dry run skip.
        paths = (os.path.join(spectrals_path, "01 Full.png"), os.path.join(spectrals_path, "01 Zoom.png"))
        return await salmon.images.upload_spectrals([(1, spectral_ids[1], paths)])

    run = _run_up(
        monkeypatch, album, torrents, args=("--dry-run",), handle_spectrals_upload_and_deletion=spectrals_with_no_skip
    )

    assert run.result.exit_code == 1
    assert "Dry run stopped before it could upload" in run.result.output
    assert image_uploads == []
    assert run.tracker.not_gets() == []
    assert os.listdir(downloads / staging.STAGING_DIR) == []


def test_refusals_from_tasks_run_together_end_the_dry_run_the_same_way(monkeypatch, dirs, image_uploads) -> None:
    library, downloads, torrents = dirs

    async def refused_in_two_tasks(*_args: Any, **_kwargs: Any) -> None:
        raise ExceptionGroup("in a task group", [DryRunRefused("first refusal"), DryRunRefused("second refusal")])

    run = _run_up(monkeypatch, _album(library / "Album"), torrents, args=("--dry-run",), mqa_test=refused_in_two_tasks)

    assert run.result.exit_code == 1
    assert "first refusal" in run.result.output
    assert os.listdir(downloads / staging.STAGING_DIR) == []


async def _refused_calls(torrents: Path) -> tuple[list[str], list[Sent]]:
    """Make each tracker call that sends something, with no skip in front of it, in a dry run."""
    tracker = FakeTracker()
    refused: list[str] = []
    async with tracker.serving():
        red = _client(RedApi, tracker, torrents)
        calls = {
            "POST": lambda: red._request("POST", red.base_url + "/ajax.php", data={"a": "b"}),
            "idempotent POST": lambda: red._request("POST", red.base_url + "/torrents.php", data={}, idempotent=True),
            "lossy master report": lambda: red.report_lossy_master(FIRST_TORRENT_ID, "comment", "WEB"),
            "RED image host": lambda: red.upload_image("cover.jpg", b"jpeg"),
        }
        try:
            with dryrun.mode():
                for name, call in calls.items():
                    try:
                        await call()
                    except DryRunRefused:
                        refused.append(name)
                # Reading still works.
                await red.api_call("browse", {"searchstr": "Artist Album"})
        finally:
            await red.close()
    return refused, tracker.sent


def test_the_guard_refuses_every_tracker_request_but_a_get(dirs) -> None:
    _library, _downloads, torrents = dirs

    refused, sent = anyio.run(_refused_calls, torrents)

    assert refused == ["POST", "idempotent POST", "lossy master report", "RED image host"]
    # Refused before anything went out, the authentication too: then the search authenticates and reads.
    assert [(request.method, request.query.get("action")) for request in sent] == [
        ("GET", "index"),
        ("GET", "browse"),
    ]


def test_the_guard_refuses_an_image_upload(image_uploads, tmp_path) -> None:
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"jpeg")

    async def upload() -> None:
        with dryrun.mode(), pytest.raises(DryRunRefused, match="upload .*cover.jpg"):
            await salmon.images.upload_cover(str(image), "testhost")

    anyio.run(upload)
    assert image_uploads == []


@pytest.mark.parametrize("host", sorted(salmon.images.HOSTS))
def test_every_image_host_refuses_to_upload_in_a_dry_run(monkeypatch, tmp_path, host: str) -> None:
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"jpeg")

    def no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the image host was contacted")

    # Only reached if the guard let the upload through.
    monkeypatch.setattr(aiohttp.ClientSession, "_request", no_network)
    monkeypatch.setattr(salmon.images.imgbox.pyimgbox, "Gallery", no_network)

    async def upload() -> None:
        uploader = salmon.images.HOSTS[host].ImageUploader()
        with dryrun.mode(), pytest.raises(DryRunRefused, match=f"upload .*cover.jpg to {host}"):
            await uploader.upload_file(str(image))

    anyio.run(upload)


def test_the_guard_refuses_a_seedbox_task() -> None:
    manager = UploadManager()

    with dryrun.mode(), pytest.raises(DryRunRefused, match="seedbox"):
        manager.add_upload_task("/music/Album", task_type="folder", is_flac=True, site_code="RED")

    assert not manager.tasks


def test_the_guard_refuses_to_log_into_a_torrent_client(monkeypatch) -> None:
    logins: list[str | None] = []
    monkeypatch.setattr(QBittorrentClient, "login", lambda client: logins.append(client.url))

    with dryrun.mode(), pytest.raises(DryRunRefused, match="log into the torrent client"):
        QBittorrentClient(url="http://127.0.0.1:9", username="user", password="secret-password")

    assert logins == []


def test_the_dry_run_flag_is_seen_by_the_tasks_it_starts_and_ends_with_its_block() -> None:
    seen: list[bool] = []

    async def look() -> None:
        seen.append(dryrun.active())

    async def run() -> None:
        with dryrun.mode():
            async with anyio.create_task_group() as tg:
                tg.start_soon(look)
        await look()

    anyio.run(run)

    assert seen == [True, False]
    assert not dryrun.active()


def test_each_upload_writes_into_its_own_scratch_directory_for_as_long_as_it_runs() -> None:
    seen: dict[str, str] = {}

    async def upload(scratch: str) -> None:
        with dryrun.writing_into(scratch):
            # Both uploads are inside their block at once.
            await anyio.sleep(0.01)
            seen[scratch] = dryrun.scratch_dir()

    async def run() -> None:
        with dryrun.mode():
            async with anyio.create_task_group() as tg:
                tg.start_soon(upload, "/scratch/run-a")
                tg.start_soon(upload, "/scratch/run-b")
            with pytest.raises(RuntimeError):
                dryrun.scratch_dir()

    anyio.run(run)

    assert seen == {"/scratch/run-a": "/scratch/run-a", "/scratch/run-b": "/scratch/run-b"}
