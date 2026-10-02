"""salmon cross-upload (#548): the requests it sends, the files it accepts, and what it does when a step fails.

Both trackers are local fakes on 127.0.0.1, and the image hosts and seedbox are in-process fakes: nothing here
reaches a real tracker or service. The OPS answers come from tests/fixtures/cross_upload (made-up release).
"""

import copy
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

import anyio
import asyncclick as click
import pytest
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
from salmon.cross_upload import (
    CrossUploadRefused,
    Release,
    _images_to_rehost,
    _input_item,
    _refuse_secrets,
    compile_data,
    is_torrent_reference,
    lossy_label,
)
from salmon.images.base import BaseImageUploader
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs
from salmon.uploader.torrent_client import QBittorrentClient
from salmon.uploader.upload import upload_footer

FIXTURES = Path(__file__).parent / "fixtures" / "cross_upload"
AUTHKEY = "authkey-0123456789"
PASSKEY = "passkey-9876543210"
API_KEYS = {"RED": "red-api-key-0001", "OPS": "ops-api-key-0001"}
SESSIONS = {"RED": "red-session-cookie", "OPS": "ops-session-cookie"}
# Torrent 600012 has a row on the fixture group page with no lossy label; 600011's says lossy master approved.
TORRENT_ID = 600012
GROUP_ID = 500002
TARGET_GROUP_ID = 900001
FOLDER = "sample3000 - SAMPLE VOL. I (RMX) (2023) [WEB FLAC]"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["response"]


# The album on disk


def _write_flac(path: Path, **tags: str) -> None:
    """Write a 16-bit 44.1 kHz FLAC with no audio, and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()


def _album(folder: Path) -> Path:
    folder.mkdir(parents=True)
    _write_flac(folder / "01. ALFA.flac", title="ALFA", artist="sample3000", tracknumber="1")
    _write_flac(folder / "02. BRAVO.flac", title="BRAVO", artist="sample3000", tracknumber="2")
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


def _ops_answer(folder: Path, torrent_id: int = TORRENT_ID) -> dict[str, Any]:
    """OPS's torrent answer for the album at folder: the fixture's WEB FLAC, with this album's files."""
    response = copy.deepcopy(_fixture("ops-torrent-web-lossy-master-approved.json"))
    response["torrent"].update(id=torrent_id, filePath=folder.name, fileList=_file_list(folder))
    response["torrent"].pop("infoHash")
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
        self.next_torrent_id = 700001
        # Answers that replace the usual one for a step, e.g. {"GET torrents.php": rate_limited}.
        self.answers: dict[str, Callable[[], web.Response]] = {}

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
            if action == "torrentgroup":
                group = self.groups.get(int(request.query["id"]))
                return _success(group) if group else _failure("bad id parameter")
            if action == "upload" and request.method == "POST":
                return self._upload(request, fields)
        if request.path == "/torrents.php" and "id" in request.query:
            return web.Response(text=self.group_page, content_type="text/html")
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
        return _success({key: value for key, value in answer.items() if key != "hash"})

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


def _client(code: str, tracker: FakeTracker, torrents: Path) -> BaseGazelleApi:
    site = {"RED": RedApi, "OPS": OpsApi}[code]()
    site.base_url = tracker.url
    site.api_key = API_KEYS[code]
    site.cookie = SESSIONS[code] if code == "OPS" else ""
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

    for name in ("testhost", "opshost", "redhost"):
        monkeypatch.setitem(salmon.images.HOSTS, name, image_host(name))
    monkeypatch.setattr(cfg.image, "cover_uploader", "testhost")
    monkeypatch.setattr(cfg.image, "image_uploader", "testhost")
    # [image.ops] and [image.red]: each tracker's own hosts, which an image for that tracker goes to.
    for code in ("ops", "red"):
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


def _within_the_plans_bound(run: Run) -> bool:
    """Whether TARGET got no more than the plan said it could: its index call aside, once per run."""
    match = re.search(r"at most (\d+) GET and (\d+) POST to RED", run.output)
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
        "GET torrents.php": 1,  # OPS's group page, for its lossy approval label
    }
    # RED has no session cookie here, so its site log is not read when the search finds nothing.
    assert run.target.steps() == {
        "GET ajax.php?action=index": 1,
        "GET ajax.php?action=browse": len(_searchstrs()),
        "POST ajax.php?action=upload": 1,
    }
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
    monkeypatch.setattr(cross_upload_module, "CONVERSIONS_CONFIRMED", True)
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
    monkeypatch.setattr(cross_upload_module, "CONVERSIONS_CONFIRMED", True)
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
    monkeypatch.setattr(cross_upload_module, "CONVERSIONS_CONFIRMED", True)
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
    monkeypatch.setattr(cross_upload_module, "CONVERSIONS_CONFIRMED", True)
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
        "GET torrents.php": 1,
    }
    assert f"torrent {TORRENT_ID} is already in this run" in run.output
    assert len(run.target.posts()) == 1


def _rate_limited() -> web.Response:
    return web.Response(status=429, headers={"Retry-After": "3600"})


@pytest.mark.parametrize("step", ["GET ajax.php?action=torrent", "GET torrents.php"])
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
    monkeypatch.setattr(cross_upload_module, "CONVERSIONS_CONFIRMED", True)
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
    assert run.source.url not in json.dumps(post.fields)


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


def test_a_link_to_the_source_tracker_in_a_description_stops_the_release() -> None:
    data = _release_data(release_desc="Transcode of [url=https://orpheus.network/torrents.php?torrentid=1]it[/url]")
    with pytest.raises(CrossUploadRefused, match="torrent description links to orpheus.network"):
        _images_to_rehost(data, OpsApi(), RedApi())


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


@pytest.mark.parametrize("media", ["BD", "Blu-Ray"])
def test_a_media_salmon_cannot_map_stops_the_release(media: str) -> None:
    response = _fixture("ops-torrent-cd-log.json")
    response["torrent"]["media"] = media
    with pytest.raises(CrossUploadRefused, match=f"has the media '{media}'"):
        cross_upload_module._check_torrent(response, OpsApi(), RedApi())


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"logScore": 95}, "its log scores 95"),
        ({"logChecksum": False}, "with no valid checksum"),
        ({"hasLog": False}, "a CD with no rip log"),
        ({"trumpable": True}, "it is trumpable on OPS"),
        ({"reported": True}, "it is reported on OPS"),
    ],
)
def test_a_flag_not_confirmed_safe_stops_the_release(change: dict[str, Any], said: str) -> None:
    response = _fixture("ops-torrent-cd-log.json")
    response["torrent"].update(change)
    with pytest.raises(CrossUploadRefused, match=said):
        cross_upload_module._check_torrent(response, OpsApi(), RedApi())


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


def test_the_torrent_description_is_the_sources_with_our_footer(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=_source_has(album))

    (post,) = run.target.posts()
    source_description = _ops_answer(album)["torrent"]["description"]
    assert post.fields["release_desc"] == [f"{source_description}\n\n{upload_footer()}"]


def test_a_description_that_already_has_a_footer_does_not_get_a_second() -> None:
    footer = "[hr]Uploaded with [url=https://github.com/chodeus/smoked-salmon][b]smoked-salmon[/b] v0.11.0[/url]"
    response = _fixture("ops-torrent-cd-log.json")
    response["torrent"]["description"] = f"Notes\n{footer}"
    assert compile_data(response, OpsApi(), RedApi())["release_desc"] == f"Notes\n{footer}"


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


def test_the_same_edition_and_format_in_the_target_group_is_flagged_and_abort_is_the_default(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    held = {
        "torrentId": 1,
        "media": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "remasterYear": 2023,
        "remasterTitle": "",
        "remasterRecordLabel": "SAMPLE GARDEN",
        "remasterCatalogueNumber": "000000000002",
    }

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album)(source, target)
        target.results = [_result([held])]

    # Pick the group, then take the default answer to "upload to this group?".
    run = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="1\n\n", prepare=prepare)

    assert "DUPE RISK: this edition already has 2023 / SAMPLE GARDEN / 000000000002 / WEB / FLAC / Lossless" in (
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


def test_a_lossy_approved_torrent_stops_before_any_target_request(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, ["600011", "-yyy"], prepare=_source_has_id(600011, album))

    assert "OPS approved it as lossy" in run.output
    assert run.result.exit_code == 1
    assert run.target.sent == []


def _source_has_id(torrent_id: int, album: Path):
    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[torrent_id] = _ops_answer(album, torrent_id)

    return prepare


@pytest.mark.parametrize(("answer", "goes"), [("\n", False), ("y\n", True)], ids=["default", "yes"])
def test_a_torrent_with_no_row_on_the_group_page_asks_and_stops_by_default(monkeypatch, dirs, answer, goes) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(
        monkeypatch,
        dirs,
        ["600099"],
        input=f"{answer}y\n\n",  # The approval question, the plan, the group
        prepare=_source_has_id(600099, album),
    )

    assert "Could not tell whether OPS approved torrent 600099" in run.output
    assert len(run.target.posts()) == (1 if goes else 0)


def test_with_yes_all_an_unknown_approval_stops_without_asking(monkeypatch, dirs) -> None:
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(monkeypatch, dirs, ["600099", "-yyy"], prepare=_source_has_id(600099, album))

    assert "Go on as not approved?" not in run.output
    assert "it may be approved as lossy on OPS" in run.output
    assert run.target.sent == []


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
    ("args", "said"),
    [
        (["1", "RED", "RED"], "must be different trackers"),
        (["1", "OPS", "DIC"], "Invalid value for 'TARGET_TRACKER': 'DIC' is not one of"),
        (["1", "2", "--path", ".", "OPS", "RED"], "--path and --group-id go with a single INPUT"),
        (["1", "--transcode", "320", "OPS", "RED"], "does not upload conversions yet"),
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
