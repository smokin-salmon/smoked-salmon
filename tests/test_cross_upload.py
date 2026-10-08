"""salmon cross-upload (#548): the requests it sends, the files it accepts, and what it does when a step fails.

Both trackers are local fakes on 127.0.0.1, and the image hosts and seedbox are in-process fakes: nothing here
reaches a real tracker or service. The OPS and RED answers come from tests/fixtures/cross_upload (made-up release).
DIC's are made up entirely, shaped like RED's: no DIC answer has been seen.
"""

import copy
import html
import json
import os
import re
import struct
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC
from test_checks_do_not_upload import write_lists  # pyright: ignore[reportMissingImports]
from torf import Torrent

import salmon.cross_upload as cross_upload_module
import salmon.images
import salmon.trackers
import salmon.uploader
import salmon.uploader.seedbox
from salmon import cfg
from salmon.config.validations import Seedbox
from salmon.cross_upload import (
    CrossUploadRefused,
    Release,
    _images_to_rehost,
    _input_item,
    _refuse_secrets,
    compile_data,
    is_torrent_reference,
    lossy_label,
    without_tracker_links,
)
from salmon.images.base import BaseImageUploader
from salmon.release_notification import get_version
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi
from salmon.trackers.dic import DICApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs
from salmon.uploader.torrent_client import QBittorrentClient
from salmon.uploader.upload import generate_torrent, upload_footer

FIXTURES = Path(__file__).parent / "fixtures" / "cross_upload"
AUTHKEY = "authkey-0123456789"
PASSKEY = "passkey-9876543210"
API_KEYS = {"RED": "red-api-key-0001", "OPS": "ops-api-key-0001"}
SESSIONS = {"RED": "red-session-cookie", "OPS": "ops-session-cookie", "DIC": "dic-session-cookie"}
# Torrent 600012 has a row on the fixture group page with no lossy label; 600011's says lossy master approved.
TORRENT_ID = 600012
GROUP_ID = 500002
TARGET_GROUP_ID = 900001
FOLDER = "sample3000 - SAMPLE VOL. I (RMX) (2023) [WEB FLAC]"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
# The lossy report comment a cross-upload offers when the torrent description shows spectrals.
SPECTRALS_COMMENT = "Spectrals are in the torrent description."


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["response"]


# The album on disk


def _write_flac(path: Path, *, rate: int = 44100, bits: int = 16, **tags: str) -> None:
    """Write a stereo FLAC with no audio (16-bit 44.1 kHz by default), and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((rate << 44) | (1 << 41) | ((bits - 1) << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()


def _album(folder: Path, *rates: int, bits: int = 16) -> Path:
    """The album: two FLACs at the given sample rates (44.1 kHz by default; the last one given goes on), a cover."""
    rates = rates or (44100,)
    folder.mkdir(parents=True)
    _write_flac(folder / "01. ALFA.flac", rate=rates[0], bits=bits, title="ALFA", artist="sample3000", tracknumber="1")
    _write_flac(
        folder / "02. BRAVO.flac", rate=rates[-1], bits=bits, title="BRAVO", artist="sample3000", tracknumber="2"
    )
    (folder / "cover.jpg").write_bytes(JPEG)
    for entry in folder.rglob("*"):
        os.utime(entry, ns=(1_000_000_000, 1_000_000_000))
    return folder


def _file_list(folder: Path) -> str:
    return "|||".join(
        f"{entry.relative_to(folder).as_posix()}{{{{{{{entry.stat().st_size}}}}}}}"
        for entry in sorted(folder.rglob("*"))
        if entry.is_file()
    )


def _snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(entry.relative_to(folder)): (entry.read_bytes() if entry.is_file() else b"dir", entry.stat().st_mtime_ns)
        for entry in sorted(folder.rglob("*"))
    }


def _dot_torrent(folder: Path) -> Torrent:
    """SOURCE's .torrent of the album at folder, as it is now."""
    torrent = Torrent(folder, trackers=["https://home.opsfet.ch/passkey/announce"], private=True, source="OPS")
    torrent.generate()
    return torrent


def _ops_answer(folder: Path, torrent_id: int = TORRENT_ID) -> dict[str, Any]:
    """OPS's torrent answer for the album at folder: the fixture's WEB FLAC, with this album's files.

    It has no infoHash: the .torrent OPS gives for it, made from the files as they are now, is known by its files.
    """
    response = copy.deepcopy(_fixture("ops-torrent-web-lossy-master-approved.json"))
    response["torrent"].update(id=torrent_id, filePath=folder.name, fileList=_file_list(folder))
    response["torrent"].pop("infoHash")
    response["dot_torrent"] = _dot_torrent(folder).dump()
    return response


# The fake trackers


@dataclass
class Sent:
    method: str
    path: str
    query: dict[str, str]
    cookie: bool
    authorization: bool
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def step(self) -> str:
        """The request as the budget names it, e.g. "GET ajax.php?action=browse"."""
        action = self.query.get("action")
        return f"{self.method} {self.path.lstrip('/')}{f'?action={action}' if action else ''}"


def _success(response: Any) -> web.Response:
    return web.json_response({"status": "success", "response": response})


def _failure(error: str) -> web.Response:
    return web.json_response({"status": "failure", "error": error})


class FakeTracker:
    """A local Gazelle tracker: SOURCE's torrents and group page, TARGET's search, groups and uploads."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.url = ""
        self.sent: list[Sent] = []
        self.torrents: dict[int, dict[str, Any]] = {}  # SOURCE answers, by torrent id
        self.group_page = (FIXTURES / "ops-group-torrent-table.html").read_text(encoding="utf-8")
        self.results: list[dict[str, Any]] = []  # TARGET search results
        self.groups: dict[int, dict[str, Any]] = {}  # TARGET torrentgroup answers
        self.uploads: list[str] = []  # How each upload POST is answered: "ok", "drop" or an error message
        self.images: dict[str, bytes] = {}  # Images on the tracker's own host, by path
        self.group_pages: dict[str, str] = {}  # The page each upload through upload.php redirects to, by torrent id
        self.next_torrent_id = 700001
        # Answers that replace the usual one for a step, e.g. {"GET torrents.php": rate_limited}.
        self.answers: dict[str, Callable[[], web.Response]] = {}
        # Each torrent's .torrent, by torrent id: an answer's "dot_torrent" unless set here.
        self.dot_torrents: dict[int, bytes] = {}

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        fields: dict[str, Any] = {}
        if request.method == "POST":
            for name, value in (await request.post()).items():
                fields.setdefault(name, []).append(value.filename if isinstance(value, web.FileField) else value)
        self.sent.append(
            Sent(
                request.method,
                request.path,
                dict(request.query),
                "session" in request.cookies,
                "Authorization" in request.headers,
                fields,
            )
        )
        if self.sent[-1].step in self.answers:
            return self.answers[self.sent[-1].step]()
        action = request.query.get("action")
        if request.path == "/ajax.php":
            if action == "index":
                return _success({"authkey": AUTHKEY, "passkey": PASSKEY})
            if action == "torrent":
                return self._torrent(request.query)
            if action == "browse":
                return _success({"results": self.results})
            if action == "download":
                return self._download(int(request.query["id"]))
            if action == "torrentgroup":
                group = self.groups.get(int(request.query["id"]))
                return _success(group) if group else _failure("bad id parameter")
            if action == "upload" and request.method == "POST":
                return self._upload(request, fields)
        if request.path == "/upload.php" and request.method == "POST":
            return self._site_upload(request, fields)
        if request.path == "/torrents.php" and action == "download":
            return self._download(int(request.query["id"]))
        if request.path == "/torrents.php" and request.query.get("torrentid") in self.group_pages:
            return web.Response(text=self.group_pages[request.query["torrentid"]], content_type="text/html")
        if request.path == "/reportsv2.php" and request.method == "POST":
            raise web.HTTPFound(f"/torrents.php?torrentid={fields['torrentid'][0]}")
        if request.path == "/torrents.php" and "id" in request.query:
            return web.Response(text=self.group_page, content_type="text/html")
        if request.path == "/torrents.php":
            return web.Response(text="<html></html>", content_type="text/html")
        if request.path == "/log.php":
            return web.Response(text="<html><body></body></html>", content_type="text/html")
        if request.path in self.images:
            return web.Response(body=self.images[request.path], content_type="image/png")
        return web.Response(status=404)

    def _torrent(self, query: Any) -> web.Response:
        if "id" in query:
            answer = self.torrents.get(int(query["id"]))
        else:
            answer = next((a for a in self.torrents.values() if a.get("hash") == query.get("hash")), None)
        if answer is None:
            return _failure("bad id parameter")
        return _success({key: value for key, value in answer.items() if key not in ("hash", "dot_torrent")})

    def _download(self, torrent_id: int) -> web.Response:
        content = self.dot_torrents.get(torrent_id) or self.torrents.get(torrent_id, {}).get("dot_torrent")
        if content is None:
            return web.Response(status=404, text="<html>Torrent not found</html>", content_type="text/html")
        return web.Response(body=content, content_type="application/x-bittorrent")

    def _upload(self, request: web.Request, fields: dict[str, Any]) -> web.StreamResponse:
        outcome = self.uploads.pop(0) if self.uploads else "ok"
        if outcome == "drop":
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        if outcome != "ok":
            return _failure(outcome)
        torrent_id, self.next_torrent_id = self.next_torrent_id, self.next_torrent_id + 1
        group_id = int(fields["groupid"][0]) if "groupid" in fields else TARGET_GROUP_ID
        return _success({"torrentid": torrent_id, "groupid": group_id})

    def _site_upload(self, request: web.Request, fields: dict[str, Any]) -> web.StreamResponse:
        """upload.php, as Gazelle's site answers it: a redirect to the new torrent on its group page."""
        outcome = self.uploads.pop(0) if self.uploads else "ok"
        if outcome == "drop":
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        if outcome != "ok":
            # The upload form again, with the error.
            return web.Response(text=f"<html><p>{outcome}</p></html>", content_type="text/html")
        torrent_id, self.next_torrent_id = self.next_torrent_id, self.next_torrent_id + 1
        group_id = int(fields["groupid"][0]) if "groupid" in fields else TARGET_GROUP_ID
        self.group_pages[str(torrent_id)] = (
            f'<html><a class="tooltip" href="torrents.php?torrentid={torrent_id}">DL</a>'
            f'<a class="brackets" href="upload.php?groupid={group_id}">Add format</a></html>'
        )
        raise web.HTTPFound(f"/torrents.php?id={group_id}&torrentid={torrent_id}")

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

    def steps(self) -> Counter[str]:
        return Counter(sent.step for sent in self.sent)

    def posts(self) -> list[Sent]:
        return [sent for sent in self.sent if sent.method == "POST"]

    def reports(self) -> list[Sent]:
        return [sent for sent in self.posts() if sent.path == "/reportsv2.php"]


def _client(code: str, tracker: FakeTracker, torrents: Path) -> BaseGazelleApi:
    site = {"RED": RedApi, "OPS": OpsApi, "DIC": DICApi}[code]()
    site.base_url = tracker.url
    # RED with its API key only, OPS with both, DIC with its session cookie only (it has no API key).
    site.api_key = API_KEYS.get(code, "")
    site.cookie = SESSIONS[code] if code != "RED" else ""
    site.dot_torrents_dir = str(torrents)
    site._rate_limiter = AsyncLimiter(1000, 1)
    return site


# A run


@dataclass
class Run:
    result: Any
    source: FakeTracker
    target: FakeTracker
    images: list[tuple[str, str]]  # (host, file name) of each image uploaded
    seeded: list[tuple[str, str]]  # (save path, torrent name) of each torrent added to the seedbox's client
    copied: list[str]  # Each folder copied to the seedbox

    @property
    def output(self) -> str:
        return self.result.output


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> SimpleNamespace:
    """A download_directory, a dot_torrents_dir and a library, configured."""
    paths = SimpleNamespace(
        downloads=tmp_path / "downloads", torrents=tmp_path / "torrents", library=tmp_path / "library"
    )
    for folder in vars(paths).values():
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(paths.downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(paths.library)])
    return paths


@pytest.fixture(autouse=True)
def settings(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload, "upload_to_seedbox", True)
    monkeypatch.setattr(cfg.upload, "torrent_name_normalization", "")
    monkeypatch.setattr(cfg.upload.requests, "check_recent_uploads", True)
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", False)
    monkeypatch.setattr(base, "_LOST_UPLOAD_FIRST_WAIT", 0)
    monkeypatch.setattr(base, "_LOST_UPLOAD_SECOND_WAIT", 0)
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])
    monkeypatch.setattr(cross_upload_module, "_check_logs", _no_log_check)


async def _no_log_check(_path: str) -> None:
    return None


def _cross_upload(
    monkeypatch,
    dirs: SimpleNamespace,
    args: list[str],
    *,
    input: str = "",
    source: str = "OPS",
    target: str = "RED",
    prepare=None,
    transcode=None,
) -> Run:
    """Run `salmon cross-upload ARGS SOURCE TARGET` against two fake trackers, a fake image host and seedbox."""
    trackers = {source: FakeTracker(source), target: FakeTracker(target)}
    images: list[tuple[str, str]] = []
    seeded: list[tuple[str, str]] = []
    copied: list[str] = []

    def image_host(name: str):
        class ImageUploader(BaseImageUploader):
            async def upload_file(self, filename: str) -> tuple[str, None]:
                images.append((name, os.path.basename(filename)))
                return f"https://{name}.images.test/{len(images)}.png", None

        return SimpleNamespace(ImageUploader=ImageUploader)

    for name in ("testhost", "opshost", "redhost", "dichost"):
        monkeypatch.setitem(salmon.images.HOSTS, name, image_host(name))
    monkeypatch.setattr(cfg.image, "cover_uploader", "testhost")
    monkeypatch.setattr(cfg.image, "image_uploader", "testhost")
    # [image.ops], [image.red] and [image.dic]: each tracker's own hosts, which an image for that tracker goes to.
    for code in ("ops", "red", "dic"):
        hosts = SimpleNamespace(cover_uploader=f"{code}host", image_uploader=f"{code}host", specs_uploader=None)
        monkeypatch.setattr(cfg.image, code, hosts)

    monkeypatch.setattr(
        cfg,
        "seedbox",
        [
            Seedbox(
                name="box",
                type="rclone",
                url="remote",
                directory="/seed",
                torrent_client="qbittorrent+http://127.0.0.1:9",
            )
        ],
    )
    monkeypatch.setattr(QBittorrentClient, "login", lambda _client: None)

    def add_to_downloader(_client, remote_folder, torrent, is_paused, label) -> bool:
        seeded.append((remote_folder, str(Torrent.read_stream(torrent).name)))
        return True

    async def rclone_copy(_seedbox, _remote, path: str) -> bool:
        copied.append(path)
        return True

    monkeypatch.setattr(QBittorrentClient, "add_to_downloader", add_to_downloader)
    monkeypatch.setattr(salmon.uploader.seedbox, "_rclone_upload_folder", rclone_copy)
    if transcode is not None:
        monkeypatch.setattr(salmon.uploader, "transcode_folder", transcode)
    monkeypatch.setattr(salmon.uploader, "check_folder_structure", _no_folder_check)

    clients: dict[str, BaseGazelleApi] = {}

    def get_class(code: str):
        def make() -> BaseGazelleApi:
            clients[code] = _client(code, trackers[code], dirs.torrents)
            return clients[code]

        return make

    monkeypatch.setattr(salmon.trackers, "get_class", get_class)

    async def run():
        async with AsyncExitStack() as stack:
            for tracker in trackers.values():
                await stack.enter_async_context(tracker.serving())
            if prepare is not None:
                prepare(trackers[source], trackers[target])
            return await CliRunner().invoke(cross_upload_module.cross_upload, [*args, source, target], input=input)

    result = anyio.run(run)
    return Run(result, trackers[source], trackers[target], images, seeded, copied)


async def _no_folder_check(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _transcode(path: str, bitrate: str, *_args: Any, output_dir: str | None = None, **_kw: Any) -> str:
    """Stands in for transcode_folder: MP3s with made-up content, where the real one writes them."""
    new_path = Path(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bitrate}]")
    new_path.mkdir(exist_ok=True)
    for name in ("01. ALFA.mp3", "02. BRAVO.mp3"):
        (new_path / name).write_bytes(bitrate.encode() * 1000)
    return str(new_path)


def _source_has(*albums: Path, answer=_ops_answer):
    """prepare: SOURCE holds a torrent for each album, ids TORRENT_ID onward."""

    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        for number, folder in enumerate(albums):
            source.torrents[TORRENT_ID + number] = answer(folder, TORRENT_ID + number)

    return prepare


def _searchstrs() -> list[str]:
    return generate_dupe_check_searchstrs([("sample3000", "main")], "SAMPLE VOL. I (RMX)", "000000000002")


def _within_the_plans_bound(run: Run, target: str = "RED") -> bool:
    """Whether TARGET got no more than the plan said it could: its index call aside, once per run."""
    match = re.search(rf"at most (\d+) GET and (\d+) POST to {target}", run.output)
    assert match is not None, run.output
    gets = sum(1 for sent in run.target.sent if sent.method == "GET" and sent.query.get("action") != "index")
    return gets <= int(match[1]) and len(run.target.posts()) <= int(match[2])


# The budget


def test_one_release_sends_what_the_budget_says(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album))

    assert run.result.exit_code == 0, run.output
    assert run.source.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=torrent": 1,
        "GET ajax.php?action=download": 1,  # The .torrent, whose pieces the files are checked against
        "GET torrents.php": 1,  # OPS's group page, for its lossy approval label
    }
    # RED has no session cookie here, so its site log is not read when the search finds nothing.
    assert run.target.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=browse": len(_searchstrs()),
        "POST ajax.php?action=upload": 1,
    }
    assert _within_the_plans_bound(run)
    # Nothing is left to fetch from OPS: the reads it already served are not part of the plan's count.
    assert ", no more GET to OPS" in run.output


def test_a_red_upload_with_a_log_goes_through_its_upload_page_and_the_budget_counts_its_redirect(
    monkeypatch, dirs
) -> None:
    name = "ops-torrent-cd-log.json"
    album = _red_album(name, dirs.downloads)
    prepare = _source_has(album, answer=_red_fixture_answer(name))
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    assert run.target.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=browse": 1,
        "POST upload.php": 1,
        "GET torrents.php": 1,  # The new torrent's page, which the upload redirects to
    }
    # The search, a group pasted at its prompt, RED's upload page for an existing group, the redirect, a lost upload.
    assert "at most 6 GET and 1 POST to RED" in run.output
    assert _within_the_plans_bound(run)


def test_the_site_log_is_read_when_the_search_is_empty_and_a_cookie_is_set(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album)(source, target)

    monkeypatch.setattr(RedApi, "has_session_cookie", property(lambda _self: True))
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    assert run.target.steps()["GET log.php"] == 9
    assert run.target.steps()["POST ajax.php?action=upload"] == 1
    assert _within_the_plans_bound(run)


def test_a_flac_and_two_transcodes_into_an_existing_group(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album)(source, target)
        target.results = [_result([])]
        target.groups[TARGET_GROUP_ID] = {"group": {"id": TARGET_GROUP_ID, "year": 2018}, "torrents": []}

    run = _cross_upload(
        monkeypatch,
        dirs,
        [str(TORRENT_ID), "-yyy", "--transcode", "320", "--transcode", "V0"],
        input="1\n\n",
        prepare=prepare,
        transcode=_transcode,
    )

    assert run.result.exit_code == 0, run.output
    assert run.source.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=torrent": 1,
        "GET ajax.php?action=download": 1,
        "GET torrents.php": 1,
    }
    assert run.target.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=browse": len(_searchstrs()),
        "GET ajax.php?action=torrentgroup": 1,  # The formats the group already has, once the FLAC is up
        "POST ajax.php?action=upload": 3,
    }
    assert _within_the_plans_bound(run)
    formats = [(post.fields["format"][0], post.fields["bitrate"][0]) for post in run.target.posts()]
    assert formats == [("FLAC", "Lossless"), ("MP3", "320"), ("MP3", "V0 (VBR)")]
    assert {post.fields["groupid"][0] for post in run.target.posts()} == {str(TARGET_GROUP_ID)}
    # The transcodes go into download_directory, and say what they were made from.
    assert sorted(path.name for path in dirs.downloads.iterdir()) == [FOLDER, f"{FOLDER} [320]", f"{FOLDER} [V0]"]
    flac_url = f"{run.target.url}/torrents.php?torrentid=700001"
    assert run.target.posts()[1].fields["release_desc"][0].startswith(f"[b]Source:[/b] {flac_url}\n")


def test_a_new_group_with_transcodes_needs_no_group_fetch(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(
        monkeypatch,
        dirs,
        [str(TORRENT_ID), "-yyy", "--all"],
        input="\n",
        prepare=_source_has(album),
        transcode=_transcode,
    )

    assert run.result.exit_code == 0, run.output
    assert "GET ajax.php?action=torrentgroup" not in run.target.steps()
    assert run.target.steps()["POST ajax.php?action=upload"] == 3


def test_five_releases_go_up_one_after_the_other(monkeypatch, dirs) -> None:
    albums = [_album(dirs.downloads / f"{FOLDER} {number}") for number in range(5)]
    ids = [str(TORRENT_ID + number) for number in range(5)]

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(*albums)(source, target)
        # Each torrent has its row, with no lossy label, on its group's page.
        source.group_page = "".join(
            source.group_page.replace(str(TORRENT_ID), str(TORRENT_ID + number)) for number in range(5)
        )

    run = _cross_upload(monkeypatch, dirs, [*ids, "-yyy"], input="\n" * 5, prepare=prepare)

    assert run.result.exit_code == 0, run.output
    assert run.source.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=torrent": 5,
        "GET ajax.php?action=download": 5,
        "GET torrents.php": 5,
    }
    assert run.target.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=browse": 5 * len(_searchstrs()),
        "POST ajax.php?action=upload": 5,
    }


def test_a_sixth_release_is_refused_before_any_request(monkeypatch, dirs) -> None:
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID + number) for number in range(6)])

    assert run.result.exit_code == 2
    assert "At most 5 releases per run" in run.output
    assert run.source.sent == run.target.sent == []


def test_a_dry_run_reads_both_trackers_and_sends_nothing(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    before = _snapshot(album)
    run = _cross_upload(
        monkeypatch,
        dirs,
        [str(TORRENT_ID), "-yyy", "--dry-run", "--transcode", "320"],
        input="\n",
        prepare=_source_has(album),
        transcode=_transcode,
    )

    assert run.result.exit_code == 0, run.output
    assert run.source.posts() == run.target.posts() == []
    assert run.target.steps()["GET ajax.php?action=browse"] == len(_searchstrs())
    assert run.output.count("Dry run: not uploading to RED.") == 2
    assert run.images == run.seeded == run.copied == []
    # Nothing left behind: no torrent file, no transcode, no scratch directory, the album as it was.
    assert list(dirs.torrents.iterdir()) == []
    assert sorted(path.name for path in dirs.downloads.iterdir()) == [".salmon-staging", FOLDER]
    assert list((dirs.downloads / ".salmon-staging").iterdir()) == []
    assert _snapshot(album) == before


# The files


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda album: (album / "02. BRAVO.flac").unlink(), "missing 02. BRAVO.flac"),
        (lambda album: (album / "notes.txt").write_text("mine"), "not in the torrent: notes.txt"),
        (lambda album: (album / "cover.jpg").write_bytes(JPEG + b"retagged"), "cover.jpg is 76 bytes, not 68"),
    ],
    ids=["missing", "extra", "resized"],
)
def test_files_that_are_not_the_torrents_stop_it_before_any_target_request(monkeypatch, dirs, change, said) -> None:
    album = _album(dirs.downloads / FOLDER)
    prepare = _source_has(album)  # The file list is the album's before the change.

    def changed(source: FakeTracker, target: FakeTracker) -> None:
        prepare(source, target)
        change(album)

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], prepare=changed)

    assert run.result.exit_code == 1
    assert said in run.output
    assert run.target.sent == []


def test_a_release_on_the_targets_do_not_upload_list_stops_before_any_target_request(monkeypatch, dirs, tmp_path):
    album = _album(dirs.downloads / FOLDER)
    write_lists(monkeypatch, tmp_path / "lists", RED="[[entry]]\nartist = 'sample3000'\nnote = 'Fakes only.'\n")

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], prepare=_source_has(album))

    assert run.result.exit_code == 1
    assert (
        f"Not cross-uploading {TORRENT_ID}: sample3000 (the whole discography) is on RED's Do-Not-Upload list: Fakes "
        "only. If yours is a legitimate copy, RED wants a message to its staff, with proof, before it is uploaded."
    ) in run.output
    assert "Nothing to cross-upload." in run.output
    assert run.target.sent == []


def test_a_listed_release_is_dropped_and_the_others_go(monkeypatch, dirs, tmp_path) -> None:
    albums = [_album(dirs.downloads / f"{FOLDER} {number}") for number in range(2)]
    # The SOURCE's list does not count: only TARGET's.
    write_lists(
        monkeypatch,
        tmp_path / "lists",
        RED="[[entry]]\nlabel = 'Bootleg Label'\nnote = 'Bootlegs.'\n",
        OPS="[[entry]]\nartist = 'sample3000'\nnote = 'Fakes only.'\n",
    )

    def answer(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = _ops_answer(folder, torrent_id)
        if torrent_id == TORRENT_ID + 1:
            response["torrent"]["remasterRecordLabel"] = "Bootleg Label"
        return response

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(*albums, answer=answer)(source, target)
        source.group_page = "".join(
            source.group_page.replace(str(TORRENT_ID), str(TORRENT_ID + number)) for number in range(2)
        )

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), str(TORRENT_ID + 1), "-yyy"], input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    assert f"Not cross-uploading {TORRENT_ID + 1}: the label Bootleg Label is on RED's Do-Not-Upload list" in run.output
    assert len(re.findall(r"^\d\. ", run.output, re.MULTILINE)) == 1
    assert run.target.steps()["POST ajax.php?action=upload"] == 1


def test_a_folder_name_with_dots_out_of_download_directory_is_refused(monkeypatch, dirs) -> None:
    outside = _album(dirs.downloads.parent / "elsewhere")

    def answer(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = _ops_answer(folder, torrent_id)
        response["torrent"]["filePath"] = "../elsewhere"
        return response

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], prepare=_source_has(outside, answer=answer))

    assert "is not a folder name" in run.output
    assert run.target.sent == []


def test_a_folder_that_is_a_link_out_of_download_directory_is_refused(monkeypatch, dirs) -> None:
    outside = _album(dirs.downloads.parent / FOLDER)
    (dirs.downloads / FOLDER).symlink_to(outside, target_is_directory=True)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], prepare=_source_has(outside))

    assert "outside download_directory" in run.output
    assert run.target.sent == []


def test_a_file_that_is_a_link_out_of_the_album_is_refused(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    secret = dirs.downloads.parent / "secret.txt"
    secret.write_text("not the album's")
    (album / "cover.jpg").unlink()
    (album / "cover.jpg").symlink_to(secret)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], prepare=_source_has(album))

    assert "cover.jpg is a link out of the album folder" in run.output
    assert run.target.sent == []


@pytest.mark.parametrize(("length", "refused"), [(180, False), (181, True)])
def test_a_path_longer_than_the_targets_limit_stops_the_release(monkeypatch, dirs, length: int, refused: bool) -> None:
    album = _album(dirs.downloads / FOLDER)
    # The whole in-torrent path counts: the folder, a slash and the file name.
    (album / f"{'x' * (length - len(FOLDER) - 1 - len('.txt'))}.txt").write_text("notes")
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album))

    assert ("1 path(s) are longer than RED's 180 characters" in run.output) is refused
    assert len(run.target.posts()) == (0 if refused else 1)


def test_a_library_album_is_read_where_it_is_and_never_written(monkeypatch, dirs) -> None:
    album = _album(dirs.library / "sample3000" / FOLDER)
    before = _snapshot(album)
    run = _cross_upload(
        monkeypatch,
        dirs,
        [str(TORRENT_ID), "-yyy", "--path", str(album), "--transcode", "V0"],
        input="\n",
        prepare=_source_has(album),
        transcode=_transcode,
    )

    assert run.result.exit_code == 0, run.output
    assert _snapshot(album) == before
    assert sorted(path.name for path in album.parent.iterdir()) == [FOLDER]
    assert (dirs.downloads / f"{FOLDER} [V0]").is_dir()
    # The FLAC's torrent is made from the library album itself, with no copy.
    assert run.seeded[0] == ("/seed", FOLDER)


def test_the_same_release_given_twice_is_read_and_uploaded_once(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    torrent = Torrent(album, trackers=["https://home.opsfet.ch/passkey/announce"], private=True, source="OPS")
    torrent.generate()
    torrent_file = dirs.torrents.parent / "source.torrent"
    torrent.write(torrent_file)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        source.torrents[TORRENT_ID] = {**_ops_answer(album), "hash": torrent.infohash.upper()}

    args = [str(TORRENT_ID), str(TORRENT_ID), str(torrent_file), "-yyy"]
    run = _cross_upload(monkeypatch, dirs, args, input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    # The repeated ID is not read again; the .torrent file is looked up, then dropped as the same torrent.
    assert run.source.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=torrent": 2,
        "GET ajax.php?action=download": 1,
        "GET torrents.php": 1,
    }
    assert f"torrent {TORRENT_ID} is already in this run" in run.output
    assert len(run.target.posts()) == 1


def _rate_limited() -> web.Response:
    return web.Response(status=429, headers={"Retry-After": "3600"})


@pytest.mark.parametrize("step", ["GET ajax.php?action=torrent", "GET ajax.php?action=download", "GET torrents.php"])
def test_a_source_failure_that_is_not_about_one_release_stops_the_run(monkeypatch, dirs, step: str) -> None:
    albums = [_album(dirs.downloads / f"{FOLDER} {number}") for number in range(2)]

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(*albums)(source, target)
        source.answers[step] = _rate_limited

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), str(TORRENT_ID + 1), "-yyy"], prepare=prepare)

    assert run.result.exit_code == 1
    assert "Stopping: OPS could not be read" in run.output
    # The second release sends nothing, and nothing goes to the target.
    assert run.source.steps()[step] == 1
    assert run.target.sent == []


# The seedbox


def test_the_seedbox_only_gets_the_new_torrent_for_files_it_already_seeds(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album))

    assert run.result.exit_code == 0, run.output
    assert run.copied == []
    assert run.seeded == [("/seed", FOLDER)]
    torrent = Torrent.read(dirs.torrents / f"{FOLDER} - RED.torrent")
    assert torrent.source == "RED"
    assert torrent.trackers == [[f"https://flacsfor.me/{PASSKEY}/announce"]]
    assert torrent.comment == f"{run.target.url}/torrents.php?torrentid=700001"


# Upload failures


def test_an_upload_whose_answer_is_lost_is_looked_up_and_never_sent_again(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album)(source, target)
        target.uploads = ["drop"]

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=prepare)

    assert run.result.exit_code == 1
    assert len(run.target.posts()) == 1
    # The lookup by infohash, twice (it was not found), then the run stops.
    lookups = [sent for sent in run.target.sent if sent.query.get("action") == "torrent"]
    assert len(lookups) == 2
    assert all("hash" in sent.query for sent in lookups)
    assert "The upload may still have gone through" in run.output
    assert run.seeded == []


def test_a_failed_second_format_stops_the_run_and_says_what_is_up(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album)(source, target)
        target.uploads = ["ok", "Your torrent is too small"]

    run = _cross_upload(
        monkeypatch,
        dirs,
        [str(TORRENT_ID), "-yyy", "--transcode", "320", "--transcode", "V0"],
        input="\n",
        prepare=prepare,
        transcode=_transcode,
    )

    assert run.result.exit_code == 1
    assert len(run.target.posts()) == 2
    assert "Your torrent is too small" in run.output
    assert f"Already uploaded:\n  {run.target.url}/torrents.php?torrentid=700001" in run.output
    # What is up is still seeded.
    assert run.seeded == [("/seed", FOLDER)]


# Images


def _red_answer(red_url: str):
    """A RED torrent answer for the album: the OPS fixture with RED-hosted, per-viewer signed images."""

    def answer(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = _ops_answer(folder, torrent_id)
        group = response["group"]
        group["wikiImage"] = f"{red_url}/i/cover.jpg?h=signature&e=1700000000&u=12345"
        group["bbBody"] = f"Notes [img]{red_url}/i/inline.png?h=other&e=1700000000&u=12345[/img]"
        response["torrent"].update(lossyMasterApproved=False, lossyWebApproved=False, remastered=True)
        return response

    return answer


def test_a_red_image_goes_to_ops_as_its_bare_url_and_is_never_fetched(monkeypatch, dirs) -> None:
    # OPS shows RED-hosted images, as a cover and in descriptions (HOST_RULES, confirmed by redusys).
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album, answer=_red_answer(source.url))(source, target)

    run = _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", source="RED", target="OPS", prepare=prepare
    )

    assert run.result.exit_code == 0, run.output
    assert not any(sent.path.startswith("/i/") for sent in run.source.sent)
    assert run.images == []
    (post,) = run.target.posts()
    assert post.fields["image"] == [f"{run.source.url}/i/cover.jpg"]
    assert post.fields["album_desc"] == [f"Notes [img]{run.source.url}/i/inline.png[/img]"]
    # RED signs the URLs it hands out per viewer, with their user id: none of that reaches OPS.
    assert "u=12345" not in json.dumps(post.fields)


def _ops_hosted_images(ops_url: str):
    """An OPS torrent answer for the album whose cover and album description are on OPS's own site."""

    def answer(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = _ops_answer(folder, torrent_id)
        response["group"]["wikiImage"] = f"{ops_url}/static/cover.jpg"
        response["group"]["wikiBBcode"] = f"Notes [img]{ops_url}/static/inline.png[/img]"
        return response

    return answer


def test_an_image_only_the_source_shows_is_fetched_through_its_client_and_rehosted_to_the_targets_host(
    monkeypatch, dirs
) -> None:
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        source.images = {"/static/cover.jpg": JPEG, "/static/inline.png": PNG}
        _source_has(album, answer=_ops_hosted_images(source.url))(source, target)

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    fetched = [sent for sent in run.source.sent if sent.path.startswith("/static/")]
    assert [sent.path for sent in fetched] == ["/static/cover.jpg", "/static/inline.png"]
    # Through OPS's client, with its session cookie, and only ever to OPS itself.
    assert all(sent.cookie and not sent.authorization for sent in fetched)
    # Uploaded to RED's own hosts: [image.red] cover_uploader and image_uploader.
    assert run.images == [("redhost", "image.jpg"), ("redhost", "image.png")]
    (post,) = run.target.posts()
    assert post.fields["image"] == ["https://redhost.images.test/1.png"]
    assert post.fields["album_desc"] == ["Notes [img]https://redhost.images.test/2.png[/img]"]
    assert f"{run.source.url}/static/" not in json.dumps(post.fields)
    assert ", 2 image GET to OPS" in run.output
    assert "no more GET" not in run.output


def test_an_image_that_is_both_the_cover_and_in_the_description_is_fetched_once(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        source.images = {"/static/cover.jpg": JPEG}
        answer = _ops_hosted_images(source.url)

        def same_image(folder: Path, torrent_id: int) -> dict[str, Any]:
            response = answer(folder, torrent_id)
            response["group"]["wikiBBcode"] = f"Notes [img]{source.url}/static/cover.jpg[/img]"
            return response

        _source_has(album, answer=same_image)(source, target)
        # RED's cover host and description image host differ: the image goes to both.
        hosts = SimpleNamespace(cover_uploader="redhost", image_uploader="testhost", specs_uploader=None)
        monkeypatch.setattr(cfg.image, "red", hosts)

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    assert run.source.steps()["GET static/cover.jpg"] == 1
    assert run.images == [("redhost", "image.jpg"), ("testhost", "image.jpg")]
    assert "images to rehost: 1, each fetched once from OPS" in run.output


def test_an_image_on_another_host_is_neither_fetched_nor_changed(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album))

    (post,) = run.target.posts()
    assert post.fields["image"] == ["https://ptpimg.me/x00012.jpg"]
    assert run.images == []


def _release_data(**changes: Any) -> dict[str, Any]:
    data = compile_data(_fixture("ops-torrent-cd-log.json"), OpsApi(), RedApi())
    return {**data, **changes}


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("See [url=https://orpheus.network/torrents.php?id=1]the CD[/url].", "See the CD."),
        ("See [URL=https://redacted.sh/artist.php?id=2]them[/URL].", "See them."),
        ("[url=torrents.php?id=1&torrentid=2]the other edition[/url]", "the other edition"),
        ("[url=/artist.php?artistname=x]the artist[/url]", "the artist"),
        ("[url=artist.php?id=3]the artist[/url] and [url=#top]top[/url]", "the artist and top"),
        ("Transcode of [url=https://orpheus.network/t?id=1]https://orpheus.network/t?id=1[/url]\n", "Transcode of \n"),
        ("[url]https://redacted.sh/torrents.php?id=1[/url] seen", " seen"),
        ("From https://redacted.sh/torrents.php?id=1&torrentid=2 and https://www.redacted.sh/x", "From  and "),
        ("[url=https://redacted.sh/forums.php]a thread", "a thread"),
        # DIC's site and its announce domain, whichever trackers the release goes between.
        ("See [url=https://dicmusic.com/torrents.php?id=1]the CD[/url].", "See the CD."),
        ("[url]https://www.dicmusic.com/x[/url]Seed https://tracker.52dic.vip/abc/announce now", "Seed  now"),
        # Kept: other sites, and images, which are not links.
        ("[url=https://www.qobuz.com/album/x]Qobuz[/url] https://bandcamp.com/x", None),
        ("[url=https://www.qobuz.com/album/x][img]https://redacted.sh/i/q.png[/img] Qobuz[/url]", None),
        ("[img]https://redacted.sh/i/a.png[/img] [img=https://redacted.sh/i/b.png]", None),
        ("[url=mailto:someone@example.com]mail[/url]", None),
    ],
)
def test_links_to_either_tracker_leave_the_descriptions_and_keep_their_text(text: str, kept: str | None) -> None:
    assert without_tracker_links(text, (OpsApi(), RedApi())) == (text if kept is None else kept)


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        # Gazelle's tags that open the site's own pages by id: on the target, the target's pages with these ids.
        ("See [torrent]123[/torrent] too", "See  too"),
        ("[torrent=noartist]https://orpheus.network/torrents.php?id=5[/torrent]", ""),
        ("[TORRENT]https://redacted.sh/torrents.php?id=5&torrentid=6[/TORRENT]", ""),
        ("Permalink: [pl]4567[/pl].", "Permalink: ."),
        ("[collage]12[/collage] [forum]3[/forum] [thread]45:678[/thread]", "  "),
        # A user or a rule of the source's site: the name or number stays, as text.
        ("Thanks [user]someone[/user], see [RULE]2.3.1[/RULE]", "Thanks someone, see 2.3.1"),
        # Inside a link to the tracker, whose text is kept.
        ("[url=https://redacted.sh/x]by [user]someone[/user][/url]", "by someone"),
        # An artist is found by name, the same on both trackers.
        ("[artist]Example Artist[/artist]", None),
    ],
)
def test_gazelle_tags_that_open_the_source_sites_pages_leave_the_descriptions(text: str, kept: str | None) -> None:
    assert without_tracker_links(text, (OpsApi(), RedApi())) == (text if kept is None else kept)


def test_a_link_wrapping_an_image_keeps_the_image() -> None:
    text = "[url=https://redacted.sh/torrents.php?id=1][img]https://ptpimg.me/x.png[/img][/url]"
    assert without_tracker_links(text, (RedApi(), OpsApi())) == "[img]https://ptpimg.me/x.png[/img]"


def test_an_album_description_linking_to_red_goes_to_ops_with_the_link_text_only(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        answer = _red_answer(source.url)

        def linked(folder: Path, torrent_id: int) -> dict[str, Any]:
            response = answer(folder, torrent_id)
            response["group"]["bbBody"] = (
                "Also in [url=https://redacted.sh/torrents.php?id=7]the deluxe edition[/url], "
                f"by [url=artist.php?id=8]the same artist[/url], [url={source.url}/collages.php?id=9]listed[/url]."
            )
            return response

        _source_has(album, answer=linked)(source, target)

    run = _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", source="RED", target="OPS", prepare=prepare
    )

    assert run.result.exit_code == 0, run.output
    (post,) = [sent for sent in run.target.posts() if sent.path == "/ajax.php"]
    assert post.fields["album_desc"] == ["Also in the deluxe edition, by the same artist, listed."]


def test_an_image_on_the_source_trackers_proxy_is_refused_rather_than_fetched() -> None:
    data = _release_data(album_desc="[img]https://imgcache.opsfet.ch/i/full/x/y[/img]")
    with pytest.raises(CrossUploadRefused, match="imgcache.opsfet.ch, which RED cannot show"):
        _images_to_rehost(data, OpsApi(), RedApi())


def test_more_than_ten_images_to_rehost_stop_the_release() -> None:
    ops = OpsApi()
    images = "".join(f"[img]{ops.base_url}/static/{number}.png[/img]" for number in range(11))
    with pytest.raises(CrossUploadRefused, match="more than 10 images"):
        _images_to_rehost(_release_data(album_desc=images), ops, RedApi())


def test_a_form_carrying_a_source_credential_is_refused() -> None:
    ops = OpsApi()
    ops.authkey = AUTHKEY
    with pytest.raises(CrossUploadRefused, match="release_desc field would carry a OPS credential"):
        _refuse_secrets(_release_data(release_desc=f"[url=x?authkey={AUTHKEY}]dl[/url]"), ops)


# The data


def test_a_release_type_the_target_lacks_stops_the_release() -> None:
    response = _fixture("ops-torrent-cd-log.json")
    response["group"]["releaseType"] = OpsApi().release_types["Split"]
    with pytest.raises(CrossUploadRefused, match="RED has no release type 'Split'"):
        compile_data(response, OpsApi(), RedApi())


def test_an_unknown_artist_role_stops_the_release_and_an_arranger_is_credited() -> None:
    response = _fixture("ops-torrent-cd-log.json")
    response["group"]["musicInfo"]["arranger"] = [{"id": 1, "name": "Arranger &amp; Co"}]
    data = compile_data(response, OpsApi(), RedApi())
    assert list(zip(data["artists[]"], data["importance[]"], strict=True)) == [
        ("Example Artist", 1),
        ("Arranger & Co", 8),
    ]

    response["group"]["musicInfo"]["lyricist"] = [{"id": 2, "name": "Someone"}]
    with pytest.raises(CrossUploadRefused, match="artist role lyricist"):
        compile_data(response, OpsApi(), RedApi())


@pytest.mark.parametrize(
    ("media", "source", "target", "sent"),
    [
        ("BD", OpsApi, RedApi, "Blu-Ray"),
        ("Blu-Ray", RedApi, OpsApi, "BD"),
        ("Vinyl", OpsApi, RedApi, "Vinyl"),
        ("BD", OpsApi, DICApi, "Blu-Ray"),
        ("Blu-Ray", DICApi, OpsApi, "BD"),
        ("Blu-Ray", DICApi, RedApi, "Blu-Ray"),
    ],
)
def test_ops_bd_and_red_and_dic_blu_ray_are_the_same_media(media: str, source, target, sent: str) -> None:
    response = _fixture("ops-torrent-cd-log.json")
    response["torrent"]["media"] = media
    notes = cross_upload_module._check_torrent(response, source(), target())
    assert compile_data(response, source(), target())["media"] == sent
    said = f"media {media} on {source().site_string} is {sent} on {target().site_string}"
    assert notes == ([] if media == sent else [said])


@pytest.mark.parametrize(
    ("fixture", "source", "target", "edition", "sent"),
    [
        # A 2023 WEB remaster with no label of its own: the group's are the original release's (RED 4847651).
        ("red-torrent-cd-log.json", RedApi, OpsApi, {"remastered": True, "remasterYear": 2023}, ("", "")),
        (
            "red-torrent-cd-log.json",
            RedApi,
            OpsApi,
            {"remastered": False, "remasterYear": 0},
            ("Example Music", "EX 00001 2"),
        ),
        # OPS has no flag: it shows its original release as an edition of the group's year with no title.
        (
            "ops-torrent-cd-log.json",
            OpsApi,
            RedApi,
            {"remasterYear": 2006, "remasterTitle": ""},
            ("Group Label", "GRP-1"),
        ),
        (
            "ops-torrent-cd-log.json",
            OpsApi,
            RedApi,
            {"remasterYear": 2006, "remasterTitle": "Store Exclusive"},
            ("", ""),
        ),
        ("ops-torrent-cd-log.json", OpsApi, RedApi, {"remasterYear": 2026, "remasterTitle": ""}, ("", "")),
    ],
    ids=["red remaster", "red original", "ops original", "ops titled edition", "ops later edition"],
)
def test_only_the_original_release_takes_the_groups_label_and_catalogue_number(
    fixture: str, source, target, edition: dict[str, Any], sent: tuple[str, str]
) -> None:
    response = _fixture(fixture)
    # OPS's group answers carry no label or catalogue number: the test gives the group some.
    response["group"].setdefault("recordLabel", "Group Label")
    response["group"].setdefault("catalogueNumber", "GRP-1")
    response["torrent"].update(remasterRecordLabel="", remasterCatalogueNumber="", **edition)

    data = compile_data(response, source(), target())
    release = Release(label="1", response=response, path=Path(), data=data)
    dupe_check_edition = cross_upload_module._edition(release)

    assert (data["remaster_record_label"], data["remaster_catalogue_number"]) == sent
    assert (dupe_check_edition["label"], dupe_check_edition["catno"]) == sent


def test_a_media_salmon_cannot_map_stops_the_release() -> None:
    response = _fixture("ops-torrent-cd-log.json")
    response["torrent"]["media"] = "Floppy"
    with pytest.raises(CrossUploadRefused, match="has the media 'Floppy'"):
        cross_upload_module._check_torrent(response, OpsApi(), RedApi())


def _change_torrent(**changes: Any) -> Callable[[dict[str, Any]], None]:
    return lambda response: response["torrent"].update(changes)


def _like_red_cd(response: dict[str, Any]) -> None:
    # RED's answer has no checksum key at all.
    response["torrent"].update(media="CD", hasLog=True, logScore=100)
    response["torrent"].pop("logChecksum")


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (_change_torrent(media="CD", hasLog=True, logScore=95, logChecksum=True), "log score 95 on OPS, checksum good"),
        (
            _change_torrent(media="CD", hasLog=True, logScore=100, logChecksum=False),
            "log score 100 on OPS, checksum missing or bad",
        ),
        (_like_red_cd, "log score 100 on OPS\n"),
        (_change_torrent(media="CD", hasLog=False, logScore=0), "a CD with no rip log on OPS"),
        (_change_torrent(trumpable=True, trumpable_reasons=["Bad tags"]), "trumpable on OPS: Bad tags"),
        (_change_torrent(trumpable=True, trumpable_reasons=[]), "trumpable on OPS\n"),
        (_change_torrent(reported=True), "reported on OPS"),
        (
            lambda response: response["group"].update(vanityHouse=True),
            "Vanity House on OPS: sent to RED without the flag",
        ),
    ],
    ids=["log 95", "bad checksum", "no checksum key", "no log", "trumpable", "trumpable no reason", "reported", "vh"],
)
def test_what_both_trackers_accept_goes_up_and_the_plan_says_it(monkeypatch, dirs, change, said: str) -> None:
    album = _album(dirs.downloads / FOLDER)

    def answer(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = _ops_answer(folder, torrent_id)
        change(response)
        return response

    run = _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album, answer=answer)
    )

    assert run.result.exit_code == 0, run.output
    assert f"   {said}" in run.output
    (post,) = run.target.posts()
    assert "vanity_house" not in post.fields


def test_an_empty_album_description_gets_the_tracklist_from_the_tags(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)

    def answer(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = _ops_answer(folder, torrent_id)
        response["group"]["wikiBBcode"] = ""
        return response

    run = _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album, answer=answer)
    )

    assert "has no album description: sending the tracklist from the tags" in run.output
    (post,) = run.target.posts()
    assert post.fields["album_desc"][0].startswith("[b][size=4]Tracklist[/size][/b]\n[b]01.[/b] sample3000 - ALFA")


def _header(source: str, target: str, uploader: str, source_url: str) -> str:
    return (
        f"[align=center][size=3][b]{source} → {target}[/b][/size]\n"
        f"[size=1]Original upload by {uploader} · [url={source_url}]View source torrent[/url]\n"
        "Cross-uploaded with [url=https://github.com/smokin-salmon/smoked-salmon]smoked-salmon[/url] "
        f"v{get_version()}[/size][/align]"
    )


def test_the_torrent_description_is_the_forks_header_then_the_sources_and_our_footer(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album))

    (post,) = run.target.posts()
    source_description = _ops_answer(album)["torrent"]["description"]
    header = _header("OPS", "RED", "uploader", f"{run.source.url}/torrents.php?torrentid={TORRENT_ID}")
    assert post.fields["release_desc"] == [f"{header}\n\n{source_description}\n\n{upload_footer()}"]


def test_the_header_links_the_uploader_to_their_profile_when_the_source_names_them() -> None:
    response = _fixture("ops-torrent-cd-log.json")
    response["torrent"].update(userId=4321, username="Some &amp; One", description="")
    ops = OpsApi()
    header = _header(
        "OPS",
        "RED",
        f"[url={ops.base_url}/user.php?id=4321]Some & One[/url]",
        f"{ops.base_url}/torrents.php?torrentid=600005",
    )
    assert compile_data(response, ops, RedApi())["release_desc"] == f"{header}\n\n{upload_footer()}"


@pytest.mark.parametrize(
    ("description", "kept"),
    [
        ("", ""),
        ("\r\n \r\n", ""),
        # Emptied by the tracker link removal.
        ("https://orpheus.network/torrents.php?id=1\r\n", ""),
        ("\r\nNotes\r\n\r\n", "Notes\n\n"),
    ],
    ids=["empty", "blank", "only a tracker link", "blank lines around"],
)
def test_the_header_the_description_and_the_footer_are_one_blank_line_apart(description: str, kept: str) -> None:
    response = _fixture("ops-torrent-cd-log.json")
    response["torrent"]["description"] = description
    header = _header("OPS", "RED", "uploader", f"{OpsApi().base_url}/torrents.php?torrentid=600005")
    assert compile_data(response, OpsApi(), RedApi())["release_desc"] == f"{header}\n\n{kept}{upload_footer()}"


def test_a_description_that_already_has_a_footer_does_not_get_a_second() -> None:
    footer = "[hr]Uploaded with [url=https://github.com/chodeus/smoked-salmon][b]smoked-salmon[/b] v0.11.0[/url]"
    response = _fixture("ops-torrent-cd-log.json")
    response["torrent"]["description"] = f"Notes\n{footer}"
    release_desc = compile_data(response, OpsApi(), RedApi())["release_desc"]
    assert release_desc.endswith(f"[/align]\n\nNotes\n{footer}")


# The dupe check


def _result(torrents: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "groupId": TARGET_GROUP_ID,
        "groupName": "SAMPLE VOL. I (RMX)",
        "artist": "sample3000",
        "groupYear": 2018,
        "releaseType": "Album",
        "tags": ["latin"],
        "torrents": torrents,
    }


def test_a_remaster_gets_its_group_as_the_default_answer(monkeypatch, dirs) -> None:
    # The fixture's edition is from 2023, its group from 2018: the group is found on the group's year (#600).
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album)(source, target)
        target.results = [_result([])]

    # The default answer to "which group?", then to "upload to this group?".
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    (post,) = run.target.posts()
    assert post.fields["groupid"] == [str(TARGET_GROUP_ID)]


@pytest.mark.parametrize(("edition", "shown"), [(2023, "(group 2018, edition 2023)"), (2018, "(2018)")])
def test_the_plan_shows_the_group_year_when_the_edition_has_another(monkeypatch, dirs, edition, shown) -> None:
    # Which year the dupe check looks for the group on, before its question.
    album = _album(dirs.downloads / FOLDER)

    def answer(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = _ops_answer(folder, torrent_id)
        response["torrent"]["remasterYear"] = edition
        return response

    run = _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album, answer=answer)
    )

    assert run.result.exit_code == 0, run.output
    assert f"1. sample3000 - SAMPLE VOL. I (RMX) {shown}, WEB\n" in run.output


@pytest.mark.parametrize(
    ("catno", "said"),
    [("000000000002", ""), ("SG-002", " (catalogue number differs: ours 000000000002)")],
    ids=["same catalogue number", "another catalogue number"],
)
def test_the_same_edition_and_format_in_the_target_group_is_flagged_and_abort_is_the_default(
    monkeypatch, dirs, catno: str, said: str
) -> None:
    # The trackers often write another catalogue number for the same release (#607).
    album = _album(dirs.downloads / FOLDER)
    held = {
        "torrentId": 1,
        "media": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "remasterYear": 2023,
        "remasterTitle": "",
        "remasterRecordLabel": "SAMPLE GARDEN",
        "remasterCatalogueNumber": catno,
    }

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album)(source, target)
        target.results = [_result([held])]

    # Pick the group, then take the default answer to "upload to this group?".
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="1\n\n", prepare=prepare)

    assert f"DUPE RISK: this edition already has 2023 / SAMPLE GARDEN / {catno} / WEB / FLAC / Lossless{said};" in (
        run.output
    )
    assert run.result.exit_code == 1
    assert run.target.posts() == []


# Lossy approval, when OPS is the source


def test_the_group_page_label_says_which_torrent_is_lossy_approved() -> None:
    page = (FIXTURES / "ops-group-torrent-table.html").read_text(encoding="utf-8")
    approved = lossy_label(page, 600011)
    not_approved = lossy_label(page, 600012)
    missing = lossy_label(page, 600099)
    login_page = lossy_label("<html><form action='login.php'></form></html>", 600011)
    assert (approved, not_approved, missing, login_page) == (True, False, None, None)


@pytest.mark.parametrize(
    "label",
    [
        '<strong class="torrent_label tl_lossyweb_approved" title="Lossy WEB Approved">Lossy WEB Approved</strong>',
        '<strong class="torrent_label tl_renamed" title="Lossy Master Approved">Lossy Master Approved</strong>',
        '<strong class="torrent_label tl_lossymaster_approved" title="Approved">Approved</strong>',
    ],
    ids=["lossy web", "class renamed", "title renamed"],
)
def test_either_the_class_or_the_title_of_the_label_says_approved(label: str) -> None:
    page = f'<table><tr class="torrent_row" id="torrent5"><td>[WEB / FLAC] {label}</td></tr></table>'
    approved = lossy_label(page, 5)
    assert approved is True


def test_a_lossy_approved_torrent_goes_up_then_is_reported_once_on_the_target(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, ["600011", "-yyy"], input="\n", prepare=_source_has_id(600011, album))

    assert run.result.exit_code == 0, run.output
    assert "approved as lossy on OPS: a lossy report goes to RED after the upload" in run.output
    assert [sent.step for sent in run.target.posts()] == [
        "POST ajax.php?action=upload",
        "POST reportsv2.php?action=takereport",
    ]
    (report,) = run.target.reports()
    # RED's report type for a WEB torrent, as `up` sends it, about the torrent just uploaded.
    assert report.fields["type"] == ["lossywebapproval"]
    assert report.fields["torrentid"] == ["700001"]
    page = f"{run.source.url}/torrents.php?torrentid=600011"
    assert report.fields["extra"][0].rstrip() == f"{SPECTRALS_COMMENT}\n\nApproved as lossy on OPS: {page}"
    assert _within_the_plans_bound(run)
    assert run.seeded == [("/seed", FOLDER)]


def test_each_conversion_of_a_lossy_approved_torrent_is_reported_too_as_up_does(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(
        monkeypatch,
        dirs,
        ["600011", "-yyy", "--transcode", "320"],
        input="\n",
        prepare=_source_has_id(600011, album),
        transcode=_transcode,
    )

    assert run.result.exit_code == 0, run.output
    assert [sent.step for sent in run.target.posts()] == [
        "POST ajax.php?action=upload",
        "POST reportsv2.php?action=takereport",
        "POST ajax.php?action=upload",
        "POST reportsv2.php?action=takereport",
    ]
    flac, mp3 = run.target.reports()
    assert mp3.fields["torrentid"] == ["700002"]
    note = f"{SPECTRALS_COMMENT}\n\nApproved as lossy on OPS: {run.source.url}/torrents.php?torrentid=600011"
    assert flac.fields["extra"][0].rstrip() == note
    assert mp3.fields["extra"][0].startswith(
        f"Transcode of {run.target.url}/torrents.php?torrentid=700001\n[hide=Lossy comment of original torrent]{note}"
    )
    assert _within_the_plans_bound(run)


def test_a_report_the_target_refuses_leaves_the_upload_and_says_how_to_report_it(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has_id(600011, album)(source, target)
        target.answers["POST reportsv2.php?action=takereport"] = lambda: web.Response(text="<html>No</html>")

    run = _cross_upload(monkeypatch, dirs, ["600011", "-yyy"], input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    assert len(run.target.reports()) == 1
    assert f"did not take the lossy master report for {run.target.url}/torrents.php?torrentid=700001" in run.output
    assert "Approved as lossy on OPS" in run.output.split("Report it by hand with this text:")[1]
    assert run.seeded == [("/seed", FOLDER)]


def test_a_dry_run_prints_the_lossy_report_it_would_send(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(
        monkeypatch, dirs, ["600011", "-yyy", "--dry-run"], input="\n", prepare=_source_has_id(600011, album)
    )

    assert run.result.exit_code == 0, run.output
    assert run.target.posts() == []
    assert "Dry run: not reporting the torrent to RED for lossy master approval. The report:" in run.output
    page = f"{run.source.url}/torrents.php?torrentid=600011"
    assert f"  {SPECTRALS_COMMENT}\n\n  Approved as lossy on OPS: {page}" in run.output


def _source_has_id(torrent_id: int, album: Path):
    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[torrent_id] = _ops_answer(album, torrent_id)

    return prepare


@pytest.mark.parametrize(
    ("answers", "reported"),
    [("\n", False), ("y\nRipped from the store\n", True)],
    ids=["default", "yes, with a comment"],
)
def test_a_torrent_with_no_row_on_the_group_page_asks_whether_to_report_it(
    monkeypatch, dirs, answers, reported
) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(
        monkeypatch,
        dirs,
        ["600099"],
        input=f"{answers}y\n\n",  # The report question (and its comment), the plan, the group
        prepare=_source_has_id(600099, album),
    )

    assert run.result.exit_code == 0, run.output
    assert "Could not tell whether OPS approved torrent 600099" in run.output
    assert "Report it as lossy on RED after the upload?" in run.output
    assert len([sent for sent in run.target.posts() if sent.path == "/ajax.php"]) == 1
    reports = [report.fields["extra"][0].rstrip() for report in run.target.reports()]
    page = f"{run.source.url}/torrents.php?torrentid=600099"
    assert reports == ([f"Ripped from the store\n\nApproved as lossy on OPS: {page}"] if reported else [])


def test_the_lossy_report_comment_question_is_about_the_cross_upload(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(
        monkeypatch,
        dirs,
        ["600011"],
        input="Ripped from the store\ny\n\n",  # The comment, the plan, the group
        prepare=_source_has_id(600011, album),
    )

    assert run.result.exit_code == 0, run.output
    assert "Comment for the lossy report on RED (it already links the torrent on OPS)" in run.output
    # Not `up`'s question, which is about go, gos and the queue.
    assert "queue" not in run.output
    (report,) = run.target.reports()
    page = f"{run.source.url}/torrents.php?torrentid=600011"
    assert report.fields["extra"][0].rstrip() == f"Ripped from the store\n\nApproved as lossy on OPS: {page}"


@pytest.mark.parametrize(
    ("args", "answer", "comment"),
    [([], "\n", SPECTRALS_COMMENT), ([], "Ripped from the store\n", "Ripped from the store"), (["-yyy"], "", None)],
    ids=["enter", "typed", "yes to all"],
)
def test_spectrals_in_the_description_prefill_the_lossy_report_comment(
    monkeypatch, dirs, args: list[str], answer: str, comment: str | None
) -> None:
    # The fixture's description has its spectrals in [hide=Spectrals]: the target's staff can check them there.
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(
        monkeypatch, dirs, ["600011", *args], input=f"{answer}y\n\n", prepare=_source_has_id(600011, album)
    )

    assert run.result.exit_code == 0, run.output
    # The default is shown in the question, which -yyy does not ask.
    assert (f"[{SPECTRALS_COMMENT}]: " in run.output) is not bool(args)
    assert "shows no spectrals" not in run.output
    (report,) = run.target.reports()
    page = f"{run.source.url}/torrents.php?torrentid=600011"
    assert report.fields["extra"][0].rstrip() == f"{comment or SPECTRALS_COMMENT}\n\nApproved as lossy on OPS: {page}"
    # Nothing is made or fetched for them: no image upload, no request beyond the usual ones.
    assert run.images == []
    assert _within_the_plans_bound(run)


@pytest.mark.parametrize(
    "description",
    ["Notes only", "Spectrals on request.", "[img]https://ptpimg.me/x00099.png[/img]"],
    ids=["no spectrals", "the word only", "an image only"],
)
@pytest.mark.parametrize("args", [[], ["-yyy"]], ids=["asked", "yes to all"])
def test_a_description_without_spectrals_says_so_and_sends_the_link_only(
    monkeypatch, dirs, description: str, args: list[str]
) -> None:
    album = _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[600011] = _ops_answer(album, 600011)
        source.torrents[600011]["torrent"]["description"] = description

    run = _cross_upload(monkeypatch, dirs, ["600011", *args], input="\ny\n\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    said = "The torrent description shows no spectrals, so the lossy report on RED should say where its staff can check"
    assert said in run.output
    assert SPECTRALS_COMMENT not in run.output
    (report,) = run.target.reports()
    page = f"{run.source.url}/torrents.php?torrentid=600011"
    assert report.fields["extra"][0].rstrip() == f"Approved as lossy on OPS: {page}"


def test_with_yes_all_an_unknown_approval_goes_up_unreported_and_names_the_page_to_check(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, ["600099", "-yyy"], input="\n", prepare=_source_has_id(600099, album))

    assert run.result.exit_code == 0, run.output
    assert "Report it as lossy" not in run.output
    page = f"{run.source.url}/torrents.php?torrentid=600099"
    assert f"Not reporting it as lossy on RED: check {page}" in run.output
    assert len(run.target.posts()) == 1
    assert run.target.reports() == []


# RED as the source, from RED's answers (tests/fixtures/cross_upload/red-*.json)


def _red_escaped(text: str) -> str:
    """A file name as RED's fileList gives it: & and anything outside ASCII as HTML entities."""
    return "".join(char if ord(char) < 128 else f"&#{ord(char)};" for char in text.replace("&", "&amp;"))


def _red_album(name: str, root: Path) -> Path:
    """The album of a RED fixture on disk: its folder and file names, unescaped, each file a few bytes.

    A FLAC is a FLAC with no audio at 16-bit 44.1 kHz: the files are read for their sample rate.
    """
    torrent = _fixture(name)["torrent"]
    folder = root / html.unescape(torrent["filePath"])
    for entry in torrent["fileList"].split("|||"):
        match = re.fullmatch(r"(.+)\{\{\{\d+\}\}\}", entry)
        assert match is not None
        path = folder / html.unescape(match[1])
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".flac":
            _write_flac(path, title=path.stem, artist="Artist")
        else:
            path.write_bytes(path.name.encode())
    return folder


def _red_fixture_answer(name: str):
    """RED's answer for a fixture's album on disk: the fixture, with the files' sizes, escaped as RED does."""

    def answer(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = copy.deepcopy(_fixture(name))
        files = sorted(entry for entry in folder.rglob("*") if entry.is_file())
        listing = "|||".join(
            f"{_red_escaped(entry.relative_to(folder).as_posix())}{{{{{{{entry.stat().st_size}}}}}}}" for entry in files
        )
        torrent = _dot_torrent(folder)
        response["torrent"].update(id=torrent_id, fileList=listing, infoHash=torrent.infohash.upper())
        response["dot_torrent"] = torrent.dump()
        return response

    return answer


def _red_to_ops(monkeypatch, dirs, name: str, prepare=None) -> Run:
    album = _red_album(name, dirs.downloads)

    def prepared(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album, answer=_red_fixture_answer(name))(source, target)
        if prepare is not None:
            prepare(source, target)

    return _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", source="RED", target="OPS", prepare=prepared
    )


def _upload_post(run: Run) -> Sent:
    (post,) = [sent for sent in run.target.posts() if sent.path == "/ajax.php"]
    return post


def test_a_red_cd_with_a_100_log_and_no_checksum_key_goes_up(monkeypatch, dirs) -> None:
    assert "logChecksum" not in _fixture("red-torrent-cd-log.json")["torrent"]
    run = _red_to_ops(monkeypatch, dirs, "red-torrent-cd-log.json")

    assert run.result.exit_code == 0, run.output
    assert "   log score 100 on RED\n" in run.output
    assert "checksum" not in run.output
    post = _upload_post(run)
    assert (post.fields["media"], post.fields["releasetype"]) == (["CD"], [str(OpsApi().release_types["Live album"])])
    # Both discs' logs go with it.
    assert sorted(unquote(name) for name in post.fields["logfiles[]"]) == [
        "Example Devotional Ensemble - Sample Devotions - CD 1.log",
        "Example Devotional Ensemble - Sample Devotions - CD 2.log",
    ]
    assert run.source.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=torrent": 1,
        "GET ajax.php?action=download": 1,  # RED, with its API key only
    }
    assert _within_the_plans_bound(run, "OPS")


def test_a_red_lossy_web_approval_goes_to_ops_as_one_lossy_approval_report(monkeypatch, dirs) -> None:
    run = _red_to_ops(monkeypatch, dirs, "red-torrent-web-lossy-web-approved.json")

    assert run.result.exit_code == 0, run.output
    assert [sent.step for sent in run.target.posts()] == [
        "POST ajax.php?action=upload",
        "POST reportsv2.php?action=takereport",
    ]
    # OPS has no lossy WEB approval: its one lossy report type.
    (report,) = run.target.reports()
    assert report.fields["type"] == ["lossyapproval"]
    assert report.fields["extra"][0].rstrip() == (
        f"{SPECTRALS_COMMENT}\n\nApproved as lossy on RED: {run.source.url}/torrents.php?torrentid={TORRENT_ID}"
    )
    assert _within_the_plans_bound(run, "OPS")


def test_reds_html_entities_are_unescaped_in_the_names_and_both_descriptions(monkeypatch, dirs) -> None:
    run = _red_to_ops(monkeypatch, dirs, "red-torrent-web-lossy-web-approved.json")

    assert run.result.exit_code == 0, run.output
    # The Thai file names matched the files on disk, and the torrent description names them in Thai.
    assert (
        dirs.downloads / "Sample Garden - SAMPLE TUNES FROM THE COAST (2018) [WEB FLAC]" / "01. ทะเลสีคราม.flac"
    ).is_file()
    post = _upload_post(run)
    assert "[b]01. ทะเลสีคราม.flac Full[/b]" in post.fields["release_desc"][0]
    assert "&#" not in post.fields["release_desc"][0]
    # So does the album description, whose broken header is fixed.
    album_desc = html.unescape(_fixture("red-torrent-web-lossy-web-approved.json")["group"]["bbBody"])
    assert "[b]01.[/b] Sample Garden - ทะเลสีคราม [i](5:20)[/i]" in album_desc
    assert post.fields["album_desc"] == [album_desc.replace("Tracklist[/b]", "Tracklist[/size][/b]", 1)]


def test_reds_descriptions_are_unescaped_before_their_tracker_links_go() -> None:
    escaped = (
        "1999 &ndash; 2023 [url=https://redacted.sh/torrents.php?id=1&amp;torrentid=2]the CD[/url] "
        "[url=https://example.com/album?id=1&amp;section=2]the shop[/url]"
    )
    unescaped = "1999 – 2023 the CD [url=https://example.com/album?id=1&section=2]the shop[/url]"
    response = _fixture("red-torrent-cd-log.json")
    response["group"]["bbBody"] += f"\n{escaped}"
    response["torrent"]["description"] = escaped

    data = compile_data(response, RedApi(), OpsApi())

    assert "Genre: Devotional, Example's Ragas/Classical, World" in data["album_desc"]
    assert "&#39;" not in data["album_desc"]
    assert "&bull;" not in data["album_desc"]
    assert data["album_desc"].endswith(f"\n{unescaped}")
    assert f"[/align]\n\n{unescaped}\n\n" in data["release_desc"]


def test_ops_descriptions_are_the_text_as_written_and_are_not_unescaped() -> None:
    # OPS's answers are not HTML-escaped (a bare "&" in its descriptions): unescaping would make "&section=" "§ion=".
    text = "R&B [url=https://example.com/album?id=1&section=2]the shop[/url], &amp; as typed"
    response = _fixture("ops-torrent-cd-log.json")
    response["group"]["wikiBBcode"] = text
    response["torrent"]["description"] = text

    data = compile_data(response, OpsApi(), RedApi())

    assert data["album_desc"] == text
    assert f"[/align]\n\n{text}\n\n" in data["release_desc"]


def test_a_red_cover_goes_to_ops_as_its_bare_url_and_is_never_fetched(monkeypatch, dirs) -> None:
    run = _red_to_ops(monkeypatch, dirs, "red-torrent-cd-log.json")

    assert run.result.exit_code == 0, run.output
    assert _upload_post(run).fields["image"] == ["https://redacted.sh/i/x00011.jpg"]
    assert run.images == []
    assert "Fetching" not in run.output


@pytest.mark.parametrize(("red", "ops"), [("Live album", "Live album"), ("DJ Mix", "DJ Mix"), ("Demo", "Demo")])
def test_a_red_release_type_is_read_from_its_id(red: str, ops: str) -> None:
    response = _fixture("red-torrent-cd-log.json")
    assert "releaseTypeName" not in response["group"]
    response["group"]["releaseType"] = RedApi().release_types[red]
    assert compile_data(response, RedApi(), OpsApi())["releasetype"] == OpsApi().release_types[ops]


def test_a_red_group_given_by_id_shows_its_held_cd_as_a_dupe(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)

    def cd_2008(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = _ops_answer(folder, torrent_id)
        response["torrent"].update(
            media="CD", remasterYear=2008, remasterRecordLabel="Example Music", remasterCatalogueNumber="EX 00001 2"
        )
        response["torrent"].update(hasLog=False)
        return response

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album, answer=cd_2008)(source, target)
        target.groups[510001] = _fixture("red-torrentgroup.json")

    # The default answer to "upload to this group?".
    run = _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy", "--group-id", "510001"], input="\n", prepare=prepare
    )

    assert "DUPE RISK: this edition already has 2008 / Example Music / EX 00001 2 / CD / FLAC / Lossless" in run.output
    assert run.result.exit_code == 1
    assert run.target.posts() == []


# DIC, from made-up answers (tests/fixtures/cross_upload/dic-torrent-web-24bit.json): no DIC answer has been seen

DIC_FIXTURE = "dic-torrent-web-24bit.json"
# What DIC's upload form takes on top of the usual fields, for the uploader's own purchase or rip.
DIC_MARKS = ("buy", "diy", "jinzhuan")


def _dic_album(root: Path, *rates: int) -> Path:
    """The DIC fixture's album on disk: its FLACs 24-bit at the given rates (96 kHz by default), its cover."""
    folder = _red_album(DIC_FIXTURE, root)
    rates = rates or (96000,)
    for number, path in enumerate(sorted(folder.glob("*.flac"))):
        _write_flac(path, rate=rates[min(number, len(rates) - 1)], bits=24, title=path.stem, artist="Example")
    return folder


def _from_dic(monkeypatch, dirs, target: str, *, args=("-yyy",), input="\n", change=None) -> Run:
    album = _dic_album(dirs.downloads)
    answer = _red_fixture_answer(DIC_FIXTURE)

    def changed(folder: Path, torrent_id: int) -> dict[str, Any]:
        response = answer(folder, torrent_id)
        if change is not None:
            change(response)
        return response

    return _cross_upload(
        monkeypatch,
        dirs,
        [str(TORRENT_ID), *args],
        input=input,
        source="DIC",
        target=target,
        prepare=_source_has(album, answer=changed),
    )


@pytest.mark.parametrize("target", ["RED", "OPS"])
def test_a_dic_torrent_goes_with_its_entities_decoded_and_its_links_to_dic_out(monkeypatch, dirs, target) -> None:
    run = _from_dic(monkeypatch, dirs, target)

    assert run.result.exit_code == 0, run.output
    # DIC's answer says nothing of a lossy approval, and its group page is not read for one.
    assert run.source.steps() == {"GET ajax.php?action=index": 1, "GET ajax.php?action=torrent": 1}
    post = _upload_post(run)
    assert post.fields["title"] == ["SAMPLE 春 & SONGS"]
    assert post.fields["album_desc"] == [
        "[b][size=4]Tracklist[/size][/b]\n[b]01.[/b] Example Ensemble - 春の星 [i](4:00)[/i]\n"
        "[b]02.[/b] Example Ensemble - Bravo & Charlie [i](3:30)[/i]\n\nAlso in the CD edition and the vinyl."
    ]
    header = _header("DIC", target, "uploader", f"{run.source.url}/torrents.php?torrentid={TORRENT_ID}")
    description = (
        "购买自 an example store & tagged by hand.\nThanks to a friend, see the thread and \n"
        "[url=https://www.qobuz.com/album/x00041]Qobuz[/url]"
    )
    assert post.fields["release_desc"] == [f"{header}\n\n{description}\n\n{upload_footer()}"]
    assert (post.fields["remaster_catalogue_number"], post.fields["bitrate"]) == (["EX-0041HR"], ["24bit Lossless"])
    assert "sample_rate" not in post.fields
    assert "lossy approval unknown on DICMusic: no lossy report" in run.output
    assert run.target.reports() == []
    assert _within_the_plans_bound(run, target)


@pytest.mark.parametrize(
    ("keys", "reported"),
    [({"lossyWebApproved": True, "lossyMasterApproved": False}, True), ({}, False)],
    ids=["RED's keys", "no keys"],
)
def test_a_dic_lossy_approval_is_read_from_reds_keys_and_otherwise_asked_for(
    monkeypatch, dirs, keys: dict[str, bool], reported: bool
) -> None:
    run = _from_dic(
        monkeypatch,
        dirs,
        "OPS",
        args=(),
        input="\ny\n\n",  # The report question or comment, the plan, the group
        change=lambda response: response["torrent"].update(keys),
    )

    assert run.result.exit_code == 0, run.output
    asked = "Report it as lossy on OPS after the upload?" in run.output
    assert asked is not reported
    assert ("Could not tell whether DICMusic approved" in run.output) is not reported
    assert len(run.target.reports()) == int(reported)


def _to_dic(monkeypatch, dirs, source: str = "OPS", *, album=None, args=("-yyy",), prepare=None, **kwargs) -> Run:
    album = album or _album(dirs.downloads / FOLDER)

    def prepared(source_tracker: FakeTracker, target_tracker: FakeTracker) -> None:
        answer = _red_answer(source_tracker.url) if source == "RED" else _ops_answer
        _source_has(album, answer=answer)(source_tracker, target_tracker)
        if prepare is not None:
            prepare(source_tracker, target_tracker)

    return _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), *args], source=source, target="DIC", prepare=prepared, **kwargs
    )


def _site_uploads(run: Run) -> list[Sent]:
    return [sent for sent in run.target.posts() if sent.path == "/upload.php"]


@pytest.mark.parametrize("source", ["OPS", "RED"])
def test_an_upload_to_dic_goes_through_its_upload_page_with_its_session_and_no_marks(monkeypatch, dirs, source) -> None:
    def prepare(source_tracker: FakeTracker, _target: FakeTracker) -> None:
        # RED's own images do not show on DIC: they are fetched from RED and rehosted.
        source_tracker.images = {"/i/cover.jpg": JPEG, "/i/inline.png": PNG}

    run = _to_dic(monkeypatch, dirs, source, input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    # Measured: the upload and the group page it redirects to. DIC has a session cookie: its site log is read
    # when the search finds nothing.
    assert run.target.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=browse": len(_searchstrs()),
        "GET log.php": 9,
        "POST upload.php": 1,
        "GET torrents.php": 1,
    }
    assert all(sent.cookie and not sent.authorization for sent in run.target.sent)
    # The bound counts each: the search, a group pasted at its prompt, the site log, the page the upload redirects
    # to, and looking up an upload whose answer was lost.
    assert f"at most {len(_searchstrs()) + 1 + 9 + 1 + 2} GET and 1 POST to DICMusic" in run.output
    assert _within_the_plans_bound(run, "DICMusic")
    (post,) = _site_uploads(run)
    assert not set(DIC_MARKS) & set(post.fields)
    assert "Self-purchased" not in run.output
    assert post.fields["release_desc"][0].startswith(f"[align=center][size=3][b]{source} → DIC[/b]")
    # A 16-bit torrent: no sample rate.
    assert "sample_rate" not in post.fields
    assert run.images == ([("dichost", "image.jpg"), ("dichost", "image.png")] if source == "RED" else [])
    assert run.seeded == [("/seed", FOLDER)]
    torrent = Torrent.read(dirs.torrents / f"{FOLDER} - DICMusic.torrent")
    assert (torrent.source, torrent.trackers) == ("DICMusic", [[f"https://tracker.52dic.vip/{PASSKEY}/announce"]])
    assert torrent.comment == f"{run.target.url}/torrents.php?torrentid=700001"


async def _converted(path: str, *, bit_depth: int, sample_rate: int, output_dir: str | None = None, **_kw: Any):
    """Stands in for convert_folder: the album's FLACs in the format asked for, where the real one writes them."""
    new_path = Path(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bit_depth}-{sample_rate}]")
    new_path.mkdir(exist_ok=True)
    for flac in sorted(Path(path).glob("*.flac")):
        _write_flac(new_path / flac.name, rate=sample_rate, bits=bit_depth, title=flac.stem, artist="sample3000")
    return sample_rate, str(new_path)


def test_a_24bit_torrent_to_dic_and_its_downconversions_each_send_their_own_sample_rate(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER, 192000, bits=24)
    monkeypatch.setattr(salmon.uploader, "convert_folder", _converted)

    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[TORRENT_ID]["torrent"]["encoding"] = "24bit Lossless"

    run = _to_dic(monkeypatch, dirs, album=album, args=("-yyy", "--downconvert"), input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    uploads = _site_uploads(run)
    assert [(post.fields["bitrate"], post.fields.get("sample_rate")) for post in uploads] == [
        (["24bit Lossless"], ["192kHz"]),
        (["24bit Lossless"], ["96kHz"]),
        (["Lossless"], None),
    ]
    # The downconversions go into the group the first upload made, through the upload page too.
    assert [sent.query.get("groupid") for sent in uploads] == [None, str(TARGET_GROUP_ID), str(TARGET_GROUP_ID)]
    assert not any(set(DIC_MARKS) & set(post.fields) for post in uploads)
    assert run.target.steps()["GET torrents.php"] == 3
    # As for one upload, with the group read for the formats it has, and a redirect per upload.
    assert f"at most {len(_searchstrs()) + 1 + 9 + 1 + 3 + 2} GET and 3 POST to DICMusic" in run.output
    assert _within_the_plans_bound(run, "DICMusic")


@pytest.mark.parametrize(
    ("rates", "said"),
    [
        ((44100, 48000), "DICMusic takes one sample rate per torrent, and the files of this one have 44.1 kHz, 48 kHz"),
        ((32000,), "The files of this torrent are 32 kHz, and DICMusic's upload form has no sample rate option for it"),
    ],
    ids=["mixed", "not on the form"],
)
def test_a_24bit_torrent_dics_form_has_no_sample_rate_for_stops_before_any_request_to_dic(
    monkeypatch, dirs, rates: tuple[int, ...], said: str
) -> None:
    album = _album(dirs.downloads / FOLDER, *rates, bits=24)

    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[TORRENT_ID]["torrent"]["encoding"] = "24bit Lossless"

    run = _to_dic(monkeypatch, dirs, album=album, input="\n", prepare=prepare)

    assert run.result.exit_code == 1
    assert f"Not cross-uploading {TORRENT_ID}: {said}" in run.output
    assert run.target.sent == []


def test_a_24bit_torrent_to_a_tracker_with_no_fields_of_its_own_has_its_files_read_for_none(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER, 96000, bits=24)

    def unread(_path: str) -> dict[str, Any]:
        raise AssertionError("the files were read")

    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[TORRENT_ID] = _ops_answer(album)
        source.torrents[TORRENT_ID]["torrent"]["encoding"] = "24bit Lossless"

    monkeypatch.setattr(cross_upload_module, "gather_audio_info", unread)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    (post,) = run.target.posts()
    assert "sample_rate" not in post.fields


def test_a_release_crediting_an_arranger_stops_before_any_request_to_dic(monkeypatch, dirs) -> None:
    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[TORRENT_ID]["group"]["musicInfo"]["arranger"] = [{"id": 1, "name": "Someone"}]

    run = _to_dic(monkeypatch, dirs, input="\n", prepare=prepare)

    assert run.result.exit_code == 1
    assert f"Not cross-uploading {TORRENT_ID}: DICMusic has no arranger role" in run.output
    assert run.target.sent == []


def test_a_dic_upload_salmon_makes_without_cross_upload_still_asks_for_the_marks(monkeypatch) -> None:
    asked: list[str] = []
    sent: list[dict[str, Any]] = []

    async def prompt(text: str, **_kwargs: Any) -> str:
        asked.append(text)
        return "p" if len(asked) == 1 else "n"

    async def upload(_site: BaseGazelleApi, data: dict[str, Any], _files: Any) -> tuple[int, int]:
        sent.append(data)
        return 1, 2

    monkeypatch.setattr(salmon.trackers.dic.click, "prompt", prompt)
    monkeypatch.setattr(BaseGazelleApi, "upload", upload)
    files: Any = None  # Never read: the upload itself is the fake above
    reposting = DICApi()
    reposting.skip_upload_marks()

    anyio.run(reposting.upload, {"title": "x"}, files)
    assert (asked, sent) == ([], [{"title": "x"}])
    anyio.run(DICApi().upload, {"title": "x"}, files)
    assert len(asked) == 2
    assert sent[-1] == {"title": "x", "buy": "on"}


# The command line


def test_a_reference_is_never_looked_up_on_disk(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "42").mkdir()
    ops = OpsApi()
    by_id = _input_item("42", ops)
    by_url = _input_item(f"{ops.base_url}/torrents.php?id=1&torrentid=42", ops)
    assert (by_id, by_url) == (42, 42)
    assert not is_torrent_reference("/srv/music/album")


@pytest.mark.parametrize("value", ["https://redacted.sh/torrents.php?torrentid=42", "album-folder", "https://"])
def test_anything_but_a_source_id_url_or_torrent_file_is_refused(value: str) -> None:
    with pytest.raises(click.UsageError):
        _input_item(value, OpsApi())


def test_a_torrent_file_is_looked_up_by_its_infohash(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    torrent = Torrent(album, trackers=["https://home.opsfet.ch/passkey/announce"], private=True, source="OPS")
    torrent.generate()
    torrent_file = dirs.torrents.parent / "source.torrent"
    torrent.write(torrent_file)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        source.torrents[TORRENT_ID] = {**_ops_answer(album), "hash": torrent.infohash.upper()}

    run = _cross_upload(monkeypatch, dirs, [str(torrent_file), "-yyy"], input="\n", prepare=prepare)

    assert run.result.exit_code == 0, run.output
    (lookup,) = [sent for sent in run.source.sent if sent.query.get("action") == "torrent"]
    assert lookup.query["hash"] == torrent.infohash.upper()


@pytest.mark.parametrize(
    ("trackers", "source_flag", "refused"),
    [
        ([], "OPS", None),
        ([], "RED", "has no announce URL and no OPS source flag"),
        ([], None, "has no announce URL and no OPS source flag"),
        (["https://flacsfor.me/passkey/announce"], "OPS", "does not announce to OPS"),
    ],
    ids=["no announce, OPS flag", "no announce, RED flag", "no announce, no flag", "announces to RED, OPS flag"],
)
def test_a_torrent_file_with_no_announce_is_known_by_its_source_flag(
    monkeypatch, dirs, trackers: list[str], source_flag: str | None, refused: str | None
) -> None:
    # qBittorrent keeps the trackers in its .fastresume and saves the .torrent without them.
    album = _album(dirs.downloads / FOLDER)
    torrent = Torrent(album, trackers=trackers, private=True, source=source_flag, created_by="qBittorrent v5.1.2")
    torrent.generate()
    torrent_file = dirs.torrents.parent / "source.torrent"
    torrent.write(torrent_file)
    if not trackers:
        assert set(Torrent.read(torrent_file).metainfo) == {"created by", "info"}

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        source.torrents[TORRENT_ID] = {**_ops_answer(album), "hash": torrent.infohash.upper()}

    run = _cross_upload(monkeypatch, dirs, [str(torrent_file), "-yyy"], input="\n", prepare=prepare)

    lookups = [sent for sent in run.source.sent if sent.query.get("action") == "torrent"]
    if refused is None:
        assert run.result.exit_code == 0, run.output
        assert [sent.query["hash"] for sent in lookups] == [torrent.infohash.upper()]
        assert len(run.target.posts()) == 1
    else:
        assert run.result.exit_code == 1
        assert f"{torrent_file} {refused}" in run.output
        assert lookups == []
        assert run.target.sent == []


@pytest.mark.parametrize(("source", "target"), [("OPS", "DIC"), ("RED", "OPS"), ("DIC", "RED")])
def test_a_torrent_salmon_made_for_the_source_and_saved_with_no_announce_is_known_by_its_flag(
    monkeypatch, dirs, source: str, target: str
) -> None:
    # The source flag salmon gives each tracker's torrents, which DIC's are assumed to carry too: no DIC torrent seen.
    album = _album(dirs.downloads / FOLDER)
    site = salmon.trackers.tracker_classes[source]()
    site.passkey, site.dot_torrents_dir = PASSKEY, str(dirs.torrents)
    _, made = generate_torrent(site, str(album), normalize=False)
    assert made.source == {"OPS": "OPS", "RED": "RED", "DIC": "DICMusic"}[source]
    made.trackers = []
    torrent_file = dirs.torrents.parent / "source.torrent"
    made.write(torrent_file)
    # Another tracker's torrent of the same files.
    other = Torrent(album, private=True, source={"OPS": "RED", "RED": "DICMusic", "DIC": "OPS"}[source])
    other.generate()
    other_file = dirs.torrents.parent / "other.torrent"
    other.write(other_file)

    def prepare(source_tracker: FakeTracker, _target: FakeTracker) -> None:
        source_tracker.torrents[TORRENT_ID] = {**_ops_answer(album), "hash": made.infohash.upper()}

    run = _cross_upload(
        monkeypatch,
        dirs,
        [str(torrent_file), str(other_file), "-yyy"],
        input="\n",
        source=source,
        target=target,
        prepare=prepare,
    )

    assert run.result.exit_code == 0, run.output
    lookups = [sent.query["hash"] for sent in run.source.sent if sent.query.get("action") == "torrent"]
    assert lookups == [made.infohash.upper()]
    assert f"{other_file} has no announce URL and no {site.site_string} source flag" in run.output
    assert len([sent for sent in run.target.posts() if sent.path in ("/ajax.php", "/upload.php")]) == 1


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["1", "RED", "RED"], "must be different trackers"),
        (["1", "OPS", "MTV"], "Invalid value for 'TARGET_TRACKER': 'MTV' is not one of"),
        (["1", "2", "--path", ".", "OPS", "RED"], "--path and --group-id go with a single INPUT"),
    ],
)
def test_the_command_line_is_checked_before_any_request(monkeypatch, args: list[str], said: str) -> None:
    monkeypatch.setattr(salmon.trackers, "get_class", lambda _code: pytest.fail("no tracker client may be made"))
    result = anyio.run(lambda: CliRunner().invoke(cross_upload_module.cross_upload, args))
    assert result.exit_code == 2
    assert said in result.output


def test_a_release_object_names_its_torrent_and_group() -> None:
    response = _fixture("ops-torrent-cd-log.json")
    release = Release(label="1", response=response, path=Path(), data={})
    assert (release.torrent["id"], release.group["id"]) == (600005, 500001)


def _to_ops(monkeypatch, dirs, album: Path, prepare=None) -> Run:
    """A cross-upload from RED to OPS of the album, the torrent described as RED does."""

    def prepared(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album, answer=_red_answer(source.url))(source, target)
        if prepare is not None:
            prepare(source, target)

    return _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", source="RED", target="OPS", prepare=prepared
    )


def test_a_16bit_torrent_above_48khz_is_refused_for_ops_before_any_request_to_it(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER, 96000)

    run = _to_ops(monkeypatch, dirs, album)

    assert run.result.exit_code == 1
    assert (
        f"Not cross-uploading {TORRENT_ID}: 2 16bit file(s) above 48 kHz: 01. ALFA.flac (96 kHz); "
        "02. BRAVO.flac (96 kHz). OPS refuses them."
    ) in run.output
    assert run.target.sent == []
    assert run.images == []


def test_a_16bit_torrent_above_48khz_goes_to_red_and_the_plan_says_it_can_be_trumped(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER, 44100, 96000)

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album))

    assert run.result.exit_code == 0, run.output
    assert "   1 16bit file(s) above 48 kHz: 02. BRAVO.flac (96 kHz). RED can trump them." in run.output
    assert len(run.target.posts()) == 1


@pytest.mark.parametrize(
    ("rate", "bits", "encoding"),
    [(48000, 16, "Lossless"), (96000, 24, "24bit Lossless")],
    ids=["16/48", "24/96"],
)
def test_a_torrent_that_is_not_16bit_above_48khz_goes_to_ops_unremarked(
    monkeypatch, dirs, rate, bits, encoding
) -> None:
    album = _album(dirs.downloads / FOLDER, rate, bits=bits)

    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[TORRENT_ID]["torrent"]["encoding"] = encoding

    run = _to_ops(monkeypatch, dirs, album, prepare)

    assert run.result.exit_code == 0, run.output
    assert "16bit file(s) above 48 kHz" not in run.output
    assert len(run.target.posts()) == 1


# The pieces (#617): a download in progress has every file at its full size, with pieces still zeroed


def _zero_a_piece(album: Path) -> None:
    """The album as a client leaves it mid-download: the cover at its full size, its data not there yet."""
    cover = album / "cover.jpg"
    cover.write_bytes(bytes(cover.stat().st_size))


def _source_has_unfinished(album: Path):
    """prepare: SOURCE has the album's torrent, made before one of its pieces was zeroed on disk."""

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album)(source, target)
        _zero_a_piece(album)

    return prepare


def _download(run: Run) -> Sent:
    (download,) = [sent for sent in run.source.sent if sent.query.get("action") == "download"]
    return download


# Measured on RED and OPS: each one's API gives the .torrent for the API key, and its download page for the session
# cookie. RED's download page answers the API key with a 401, and the passkey alone with its login page.


@pytest.mark.parametrize("source", ["RED", "OPS"], ids=["RED, API key only", "OPS, API key and cookie"])
def test_with_an_api_key_the_torrent_is_downloaded_through_the_api_without_a_token(
    monkeypatch, dirs, source: str
) -> None:
    if source == "RED":
        run = _red_to_ops(monkeypatch, dirs, "red-torrent-web-lossy-web-approved.json")
    else:
        album = _album(dirs.downloads / FOLDER)
        run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album))

    assert run.result.exit_code == 0, run.output
    download = _download(run)
    assert (download.method, download.path, download.query) == (
        "GET",
        "/ajax.php",
        {"action": "download", "id": str(TORRENT_ID)},
    )
    assert (download.authorization, download.cookie) == (True, False)
    assert "pieces differ" not in run.output


def test_with_a_session_cookie_only_the_torrent_is_downloaded_through_the_site_link(monkeypatch, dirs) -> None:
    monkeypatch.setitem(API_KEYS, "OPS", "")
    album = _album(dirs.downloads / FOLDER)
    # Every request is printed, secrets masked.
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", True)
    run = _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy", "--dry-run"], input="\n", prepare=_source_has(album)
    )

    assert run.result.exit_code == 0, run.output
    download = _download(run)
    assert (download.method, download.path, download.query) == (
        "GET",
        "/torrents.php",
        {"action": "download", "id": str(TORRENT_ID), "torrent_pass": PASSKEY},
    )
    assert (download.authorization, download.cookie) == (False, True)
    assert PASSKEY not in run.output
    assert '"torrent_pass":"[REDACTED]"' in run.output  # The download was printed, masked


@pytest.mark.parametrize("args", [["-yyy"], []], ids=["yes to all", "asked, default"])
def test_a_zeroed_piece_stops_the_release_before_any_target_request(monkeypatch, dirs, args: list[str]) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), *args], input="\n", prepare=_source_has_unfinished(album))

    assert run.result.exit_code == 1
    assert "1 of 1 pieces differ from the OPS torrent: is the download complete?" in run.output
    assert f"Not cross-uploading {TORRENT_ID}: 1 of 1 pieces differ from the OPS torrent\n" in run.output
    assert ("Cross-upload it anyway?\x1b[0m [y/N]" in run.output) == (not args)
    assert run.target.sent == []


def test_the_user_can_cross_upload_a_release_whose_pieces_differ(monkeypatch, dirs) -> None:
    # A copy retagged within its tag padding has the torrent's sizes and other pieces.
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID)], input="y\n\n\n", prepare=_source_has_unfinished(album))

    assert run.result.exit_code == 0, run.output
    assert "   1 of 1 pieces differ from the OPS torrent: going anyway\n" in run.output
    assert run.target.steps()["POST ajax.php?action=upload"] == 1


def test_a_torrent_file_input_is_checked_against_itself_with_no_download(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    torrent = _dot_torrent(album)
    torrent_file = dirs.torrents.parent / "source.torrent"
    torrent.write(torrent_file)
    _zero_a_piece(album)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        source.torrents[TORRENT_ID] = {**_ops_answer(album), "hash": torrent.infohash.upper()}

    run = _cross_upload(monkeypatch, dirs, [str(torrent_file), "-yyy"], prepare=prepare)

    assert run.result.exit_code == 1
    assert "1 of 1 pieces differ from the OPS torrent" in run.output
    assert "GET ajax.php?action=download" not in run.source.steps()
    assert run.target.sent == []


@pytest.mark.parametrize("source", ["OPS", "RED"], ids=["by its files", "by its infohash"])
def test_a_downloaded_torrent_that_is_another_ones_stops_the_release(monkeypatch, dirs, source: str) -> None:
    # OPS's answers here have no infoHash, RED's have one.
    name = "red-torrent-web-lossy-web-approved.json"
    album = _album(dirs.downloads / FOLDER) if source == "OPS" else _red_album(name, dirs.downloads)
    answer = _ops_answer if source == "OPS" else _red_fixture_answer(name)
    # Another release's .torrent, with a file of another size; RED's, another torrent of these same files.
    other_album = _album(dirs.torrents.parent / "other" / FOLDER)
    (other_album / "cover.jpg").write_bytes(JPEG * 2)
    other = _dot_torrent(other_album if source == "OPS" else album)
    other.source = "another"

    def prepare(source_tracker: FakeTracker, target: FakeTracker) -> None:
        _source_has(album, answer=answer)(source_tracker, target)
        other.generate()
        source_tracker.dot_torrents[TORRENT_ID] = other.dump()

    target = "OPS" if source == "RED" else "RED"
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], source=source, target=target, prepare=prepare)

    assert run.result.exit_code == 1
    assert f"Not cross-uploading {TORRENT_ID}: the .torrent {source} gave is not this torrent's" in run.output
    assert run.target.sent == []


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (
            lambda: web.Response(text=f"<html>Not a torrent, {PASSKEY}</html>", content_type="text/html"),
            "what OPS gave for its .torrent is not one",
        ),
        (
            lambda: web.Response(status=404, text=f"<html>Not found, {PASSKEY}</html>", content_type="text/html"),
            "<html>Not found, [REDACTED]</html>",
        ),
        (
            lambda: web.json_response({"status": "failure", "error": "bad credentials"}, status=401),
            "OPS did not give its .torrent: give the .torrent file as INPUT",
        ),
    ],
    ids=["a page", "not found", "401"],
)
def test_a_download_that_gives_no_torrent_stops_only_that_release(monkeypatch, dirs, answer, said: str) -> None:
    albums = [_album(dirs.downloads / f"{FOLDER} {number}") for number in range(2)]

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(*albums)(source, target)
        source.answers["GET ajax.php?action=download"] = answer

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), str(TORRENT_ID + 1), "-yyy"], prepare=prepare)

    assert run.result.exit_code == 1
    assert f"Not cross-uploading {TORRENT_ID}: {said}\n" in run.output
    assert f"Not cross-uploading {TORRENT_ID + 1}: {said}\n" in run.output
    assert run.source.steps()["GET ajax.php?action=download"] == 2
    assert PASSKEY not in run.output
    assert run.target.sent == []


def test_a_check_that_takes_more_than_a_second_shows_its_progress(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    seconds = iter(range(1000))
    monkeypatch.setattr(cross_upload_module, "monotonic", lambda: next(seconds))
    run = _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy", "--dry-run"], input="\n", prepare=_source_has(album)
    )

    assert run.result.exit_code == 0, run.output
    assert "\rChecking pieces: 1 of 1\n" in run.output


def test_a_dic_torrent_by_id_is_checked_by_size_only_and_the_plan_says_so(monkeypatch, dirs) -> None:
    run = _from_dic(monkeypatch, dirs, "RED")

    assert run.result.exit_code == 0, run.output
    assert "GET ajax.php?action=download" not in run.source.steps()
    assert "   files checked by size only: give the DICMusic .torrent to check pieces\n" in run.output


def test_the_pieces_are_checked_before_the_audio_is_read_for_the_16bit_rule(monkeypatch, dirs) -> None:
    # A file still downloading may not even read as audio: the 16bit rule must only see complete files.
    album = _album(dirs.downloads / FOLDER, 96000)

    def unfinished(_source: FakeTracker, _target: FakeTracker) -> None:
        flac = album / "01. ALFA.flac"
        flac.write_bytes(bytes(flac.stat().st_size))

    def unread(_path: str) -> dict[str, Any]:
        raise AssertionError("the files were read")

    monkeypatch.setattr(cross_upload_module, "gather_audio_info", unread)
    run = _to_ops(monkeypatch, dirs, album, unfinished)

    assert run.result.exit_code == 1
    assert f"Not cross-uploading {TORRENT_ID}: 1 of 1 pieces differ from the RED torrent\n" in run.output
    assert "16bit file(s) above 48 kHz" not in run.output
    assert run.target.sent == []


def test_downconvert_of_a_release_with_mixed_sample_rates_is_refused_before_any_request_to_target(
    monkeypatch, dirs
) -> None:
    album = _album(dirs.downloads / FOLDER, 96000, 44100, bits=24)

    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[TORRENT_ID] = _ops_answer(album)
        source.torrents[TORRENT_ID]["torrent"]["encoding"] = "24bit Lossless"

    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy", "--downconvert"], input="\n", prepare=prepare)

    assert run.result.exit_code == 1
    assert f"Not cross-uploading {TORRENT_ID}: --downconvert: the files have different sample rates" in run.output
    assert run.target.sent == []
