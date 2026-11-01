"""Helpers of tests/test_cross_upload.py that other test modules use too."""

import copy
import json
import os
import struct
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
from aiohttp import web
from aiolimiter import AsyncLimiter
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC
from torf import Torrent

import salmon.cross_upload as cross_upload_module
import salmon.images
import salmon.trackers
import salmon.uploader
import salmon.uploader.seedbox
from salmon import cfg
from salmon.config.validations import Seedbox
from salmon.images.base import BaseImageUploader
from salmon.trackers.base import BaseGazelleApi
from salmon.trackers.dic import DICApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader.torrent_client import QBittorrentClient

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cross_upload"


AUTHKEY = "authkey-0123456789"


PASSKEY = "passkey-9876543210"


API_KEYS = {"RED": "red-api-key-0001", "OPS": "ops-api-key-0001"}


SESSIONS = {"RED": "red-session-cookie", "OPS": "ops-session-cookie", "DIC": "dic-session-cookie"}


# The credentials RED's image host takes on an image URL, as RED's ajax.php?action=imgauth gives them: key, expiry
# (far off, so never expired) and user id.
IMAGE_AUTH = {"h": "LPYgXhmKIT2Ta4PZzQAcdR", "e": "4102444800", "u": "4242424"}


# Torrent 600012 has a row on the fixture group page with no lossy label; 600011's says lossy master approved.
TORRENT_ID = 600012


TARGET_GROUP_ID = 900001


FOLDER = "sample3000 - SAMPLE VOL. I (RMX) (2023) [WEB FLAC]"


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["response"]


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


@dataclass
class Sent:
    method: str
    path: str
    query: dict[str, str]
    cookie: bool
    authorization: bool
    fields: dict[str, Any] = field(default_factory=dict)
    # The content of each file part, by field name.
    files: dict[str, list[bytes]] = field(default_factory=dict)

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
        # How each upload POST is answered: "ok", "drop", "429" or an error message.
        self.uploads: list[str] = []
        self.report_answers: list[str] = []  # How each report POST is answered: "ok" or "drop"
        self.images: dict[str, bytes] = {}  # Images on the tracker's own host, by path
        self.group_pages: dict[str, str] = {}  # The page each upload through upload.php redirects to, by torrent id
        self.next_torrent_id = 700001
        # Answers that replace the usual one for a step, e.g. {"GET torrents.php": rate_limited}.
        self.answers: dict[str, Callable[[], web.Response]] = {}
        # Each torrent's .torrent, by torrent id: an answer's "dot_torrent" unless set here.
        self.dot_torrents: dict[int, bytes] = {}
        # The credentials its image host (/i/) answers only with, on the image URL, which ajax.php?action=imgauth
        # gives: RED's.
        self.image_auth = dict(IMAGE_AUTH) if code == "RED" else {}

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        fields: dict[str, Any] = {}
        files: dict[str, list[bytes]] = {}
        if request.method == "POST":
            for name, value in (await request.post()).items():
                if isinstance(value, web.FileField):
                    fields.setdefault(name, []).append(value.filename)
                    files.setdefault(name, []).append(value.file.read())
                else:
                    fields.setdefault(name, []).append(value)
        self.sent.append(
            Sent(
                request.method,
                request.path,
                dict(request.query),
                "session" in request.cookies,
                "Authorization" in request.headers,
                fields,
                files,
            )
        )
        if self.sent[-1].step in self.answers:
            return self.answers[self.sent[-1].step]()
        action = request.query.get("action")
        if request.path == "/ajax.php":
            if action == "index":
                return _success({"authkey": AUTHKEY, "passkey": PASSKEY})
            if action == "imgauth" and self.image_auth:
                auth = self.image_auth
                return _success({"h": auth["h"], "e": int(auth["e"]), "u": int(auth["u"])})
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
            if (self.report_answers.pop(0) if self.report_answers else "ok") == "drop":
                return self._drop(request)
            raise web.HTTPFound(f"/torrents.php?torrentid={fields['torrentid'][0]}")
        if request.path == "/torrents.php" and "id" in request.query:
            return web.Response(text=self.group_page, content_type="text/html")
        if request.path == "/torrents.php":
            return web.Response(text="<html></html>", content_type="text/html")
        if request.path == "/log.php":
            return web.Response(text="<html><body></body></html>", content_type="text/html")
        # RED's image host: signed on the URL, whatever else the request carries.
        signed = {name: request.query.get(name) for name in IMAGE_AUTH} == self.image_auth
        if self.image_auth and request.path.startswith("/i/") and not signed:
            return web.Response(status=403, text="<html>403 Forbidden</html>", content_type="text/html")
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

    @staticmethod
    def _drop(request: web.Request) -> web.StreamResponse:
        """Close the connection without an answer, once the request has arrived."""
        assert request.transport is not None
        request.transport.close()
        return web.Response()

    def _upload(self, request: web.Request, fields: dict[str, Any]) -> web.StreamResponse:
        outcome = self.uploads.pop(0) if self.uploads else "ok"
        if outcome == "drop":
            return self._drop(request)
        if outcome == "429":
            return web.json_response({"status": "failure"}, status=429, headers={"Retry-After": "1"})
        if outcome != "ok":
            return _failure(outcome)
        torrent_id, self.next_torrent_id = self.next_torrent_id, self.next_torrent_id + 1
        group_id = int(fields["groupid"][0]) if "groupid" in fields else TARGET_GROUP_ID
        return _success({"torrentid": torrent_id, "groupid": group_id})

    def _site_upload(self, request: web.Request, fields: dict[str, Any]) -> web.StreamResponse:
        """upload.php, as Gazelle's site answers it: a redirect to the new torrent on its group page."""
        outcome = self.uploads.pop(0) if self.uploads else "ok"
        if outcome == "drop":
            return self._drop(request)
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


def _client(
    code: str, tracker: FakeTracker, torrents: Path, *, unlimited: bool = True, red_session: bool = False
) -> BaseGazelleApi:
    """A real client of the tracker, sending to the fake. `unlimited=False` keeps the account's own rate limit."""
    site = {"RED": RedApi, "OPS": OpsApi, "DIC": DICApi}[code]()
    site.base_url = tracker.url
    # RED with its API key only (and its session cookie with red_session), OPS with both, DIC with its session cookie
    # only (it has no API key).
    site.api_key = API_KEYS.get(code, "")
    site.cookie = SESSIONS[code] if code != "RED" or red_session else ""
    site.dot_torrents_dir = str(torrents)
    if unlimited:
        site._rate_limiter = AsyncLimiter(1000, 1)
    return site


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


@dataclass
class World:
    """Two fake trackers (not serving yet), and what the fake image hosts and seedbox got."""

    trackers: dict[str, FakeTracker]
    images: list[tuple[str, str]]
    seeded: list[tuple[str, str]]
    copied: list[str]

    @asynccontextmanager
    async def serving(self) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for tracker in self.trackers.values():
                await stack.enter_async_context(tracker.serving())
            yield


def cross_upload_world(
    monkeypatch,
    dirs: SimpleNamespace,
    *,
    source: str = "OPS",
    target: str = "RED",
    transcode=None,
    unlimited: bool = True,
    red_session: bool = False,
) -> World:
    """Send every tracker client to a fake tracker of its own, and every image and seedbox step to fakes.

    `unlimited=False` keeps each tracker account's own rate limit. `red_session` gives RED's client its session
    cookie as well as its API key.
    """
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

    def get_class(code: str):
        return lambda: _client(code, trackers[code], dirs.torrents, unlimited=unlimited, red_session=red_session)

    monkeypatch.setattr(salmon.trackers, "get_class", get_class)
    return World(trackers, images, seeded, copied)


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
    red_session: bool = False,
) -> Run:
    """Run `salmon cross-upload ARGS SOURCE TARGET` against two fake trackers, a fake image host and seedbox."""
    world = cross_upload_world(
        monkeypatch, dirs, source=source, target=target, transcode=transcode, red_session=red_session
    )
    trackers = world.trackers

    async def run():
        async with world.serving():
            if prepare is not None:
                prepare(trackers[source], trackers[target])
            return await CliRunner().invoke(cross_upload_module.cross_upload, [*args, source, target], input=input)

    result = anyio.run(run)
    return Run(result, trackers[source], trackers[target], world.images, world.seeded, world.copied)


async def _no_folder_check(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _transcode(path: str, bitrate: str, *_args: Any, output_dir: str | None = None, **_kw: Any) -> str:
    """Stands in for transcode_folder: MP3s with made-up content, where the real one writes them."""
    new_path = Path(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bitrate}]")
    new_path.mkdir(exist_ok=True)
    for name in ("01. ALFA.mp3", "02. BRAVO.mp3"):
        (new_path / name).write_bytes(bitrate.encode() * 1000)
    return str(new_path)


async def _converted(path: str, *, bit_depth: int, sample_rate: int, output_dir: str | None = None, **_kw: Any):
    """Stands in for convert_folder: the album's FLACs in the format asked for, where the real one writes them."""
    new_path = Path(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bit_depth}-{sample_rate}]")
    new_path.mkdir(exist_ok=True)
    for flac in sorted(Path(path).glob("*.flac")):
        _write_flac(new_path / flac.name, rate=sample_rate, bits=bit_depth, title=flac.stem, artist="sample3000")
    return sample_rate, str(new_path)
