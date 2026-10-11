"""salmon web's description generator, image uploads and spectrals upload (#657, ADR 0004).

Image hosts are public, so what goes to them is only ever an image: the tests that matter here are the ones where
a file that is not one is refused and the fake host receives nothing. Every host and store is a fake on loopback or
a stand-in module; the stores are reached through the test seam of the SSRF guard (``ssrf.ALLOWED``), and the
network guard in conftest.py stands. A spectrals upload runs on a stand-in for the spectrals job (sox and the
frequency analysis are tested with it in test_webui_pages.py) that writes real PNGs, since the upload reads them.
"""

import asyncio
import contextlib
import dataclasses
import io
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import msgspec
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from asyncclick.testing import CliRunner
from PIL import Image
from test_image_upload_connections import REAL_HOSTS, _redirect  # pyright: ignore[reportMissingImports]
from test_ssrf import LOOPBACK, FakeStore, bandcamp_page  # pyright: ignore[reportMissingImports]
from test_webui_jobs import (  # noqa: F401  # pyright: ignore[reportMissingImports]
    AUTH,
    Params,
    _answer,
    _finished,
    _gate,
    _question,
    _until,
    _with_app,
)
from test_webui_jobs import kind as register_job_kind  # noqa: F401  # pyright: ignore[reportMissingImports]

import salmon.commands
import salmon.images
import salmon.trackers
from salmon import cfg, interaction, ssrf
from salmon.config.validations import GazelleTrackerSettings, ImageUploader, Seedbox
from salmon.tagger.sources import METASOURCES
from salmon.trackers import base
from salmon.webui import jobs, paths
from salmon.webui.jobs import JobManager

PASSKEY = "planted-passkey-0123456789abcdef0123"
PLANTED_KEYS = {
    "imgbb_key": "planted-imgbb-key-71c2",
    "ptscreens_key": "planted-ptscreens-key-9d40",
    "oeimg_key": "planted-oeimg-key-3b8e",
    "ra_key": "planted-ra-key-e5a7",
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_learned_secrets", set())
    # The clipboard is nobody's on a server (and not this test run's): a copy is a failure unless a test expects it.
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", False)


# --- Files and folders ------------------------------------------------------------------------------------


def png(color: str = "red") -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(data, "PNG")
    return data.getvalue()


def jpeg(color: str = "blue") -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(data, "JPEG")
    return data.getvalue()


def truncated_jpeg() -> bytes:
    rng = random.Random(1)
    noise = Image.frombytes("RGB", (64, 64), rng.randbytes(64 * 64 * 3))
    data = io.BytesIO()
    noise.save(data, "JPEG")
    whole = data.getvalue()
    return whole[: len(whole) // 2]


def torrent() -> bytes:
    """A .torrent as a tracker gives it: the announce URL carries the user's passkey."""
    announce = f"https://tracker.example/{PASSKEY}/announce".encode()
    return b"d8:announce" + str(len(announce)).encode() + b":" + announce + b"4:infod6:lengthi3e4:name1:aee"


@dataclass
class World:
    downloads: Path
    library: Path
    outside: Path
    scratch: Path
    torrents: Path
    seedbox: Path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """The roots, a dot_torrents_dir and a local seedbox's folder inside download_directory, and a folder outside."""
    found = World(
        downloads=tmp_path / "downloads",
        library=tmp_path / "library",
        outside=tmp_path / "outside",
        scratch=tmp_path / "scratch",
        torrents=tmp_path / "downloads" / "torrents",
        seedbox=tmp_path / "downloads" / "seedbox",
    )
    for folder in (found.downloads, found.library, found.outside, found.scratch, found.torrents, found.seedbox):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(found.downloads))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(found.library)])
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(found.scratch))
    monkeypatch.setattr(cfg.directory, "dottorrents_dir", str(found.torrents))
    monkeypatch.setattr(cfg, "seedbox", [_seedbox(str(found.seedbox))])
    return found


def _seedbox(directory: str) -> Seedbox:
    return Seedbox(name="box", type="local", directory=directory)


# --- An image host that records what it is sent --------------------------------------------------------------


@dataclass
class Received:
    filename: str
    data: bytes
    fields: list[str]


@dataclass
class FakeHost:
    """A host that answers as catbox does, and keeps everything it is sent. `echo` is added to every URL."""

    received: list[Received] = field(default_factory=list)
    requests: int = 0
    echo: str = ""

    @asynccontextmanager
    async def serving(self) -> AsyncIterator[str]:
        app = web.Application(client_max_size=64 * 1024**2)
        app.router.add_post("/{tail:.*}", self._upload)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            yield f"http://127.0.0.1:{runner.addresses[0][1]}"
        finally:
            await runner.cleanup()

    async def _upload(self, request: web.Request) -> web.Response:
        self.requests += 1
        form = await request.post()
        files = [field for field in form.values() if isinstance(field, web.FileField)]
        for upload in files:
            self.received.append(Received(unquote(upload.filename), upload.file.read(), sorted(key for key in form)))
        return web.Response(text=f"https://img.example/{unquote(files[0].filename)}{self.echo}")

    def sent(self) -> list[tuple[str, bytes, list[str]]]:
        return sorted((each.filename, each.data, each.fields) for each in self.received)


def _fake_catbox(monkeypatch: pytest.MonkeyPatch, host_url: str) -> None:
    _redirect(monkeypatch, {REAL_HOSTS[0]: host_url})


# --- Running jobs ---------------------------------------------------------------------------------------------


async def _post(client: TestClient, kind: str, params: dict[str, Any], **body: Any) -> Any:
    return await client.post("/api/jobs", json={"kind": kind, "params": params, **body}, headers=AUTH)


async def _run(client: TestClient, manager: JobManager, kind: str, params: dict[str, Any], **body: Any) -> jobs.Job:
    response = await _post(client, kind, params, **body)
    assert response.status == 201, await response.text()
    return await _finished(manager, (await response.json())["id"])


async def _refused(client: TestClient, kind: str, params: dict[str, Any], status: int = 422) -> str:
    """The detail of a job refused before it was queued."""
    response = await _post(client, kind, params)
    assert response.status == status, await response.text()
    return (await response.json())["detail"]


@contextlib.asynccontextmanager
async def _events(client: TestClient) -> AsyncIterator[list[str]]:
    """Every event the server sends from now on, as text."""
    seen: list[str] = []
    async with client.ws_connect("/api/ws", headers=AUTH) as socket:

        async def read() -> None:
            async for message in socket:
                seen.append(str(message.data))

        reading = asyncio.create_task(read())
        try:
            yield seen
        finally:
            await asyncio.sleep(0.1)
            reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reading


async def _nothing_queued(client: TestClient, manager: JobManager) -> None:
    assert manager.jobs == {}
    assert (await (await client.get("/api/jobs", headers=AUTH)).json())["jobs"] == []


# --- Image uploads: only images ---------------------------------------------------------------------------------

NOT_IMAGES = {
    "a real .torrent": ("announce.torrent", torrent()),
    "the same .torrent renamed cover.jpg": ("cover.jpg", torrent()),
    "a .log": ("rip.log", b"Exact Audio Copy V1.6\nTrack 1\n"),
    "a text file": ("notes.txt", b"hello"),
    "a text file called .png": ("notes.png", b"hello"),
    "a .cue": ("album.cue", b'FILE "01.flac" WAVE\n'),
    "an archive": ("scans.zip", b"PK\x03\x04" + bytes(40)),
    "a zero-byte .png": ("empty.png", b""),
    "a truncated JPEG": ("cut.jpg", truncated_jpeg()),
}


@pytest.mark.parametrize("name", NOT_IMAGES)
def test_a_file_that_is_not_an_image_is_refused_and_the_host_receives_nothing(
    monkeypatch: pytest.MonkeyPatch, world: World, name: str
) -> None:
    filename, content = NOT_IMAGES[name]
    good = world.downloads / "good.png"
    good.write_bytes(png())
    bad = world.downloads / filename
    bad.write_bytes(content)
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url, _events(client) as events:
            _fake_catbox(monkeypatch, url)
            # Alone, and among good images: one bad file refuses the job.
            for picked in ([bad], [good, bad]):
                response = await _post(client, "images", {"paths": [str(path) for path in picked]})
                body = await response.text()
                assert response.status == 422, body
                assert filename in body
                assert PASSKEY not in body
            await _nothing_queued(client, manager)
        assert host.requests == 0
        assert PASSKEY not in " ".join(events)

    _with_app(test)


def test_a_file_that_is_not_an_image_is_refused_even_when_it_looks_like_one_to_the_extension_check(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    # A PNG called .torrent is an image, by what is in it: the name decides nothing either way.
    odd = world.downloads / "picture.torrent"
    odd.write_bytes(png())
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url:
            _fake_catbox(monkeypatch, url)
            job = await _run(client, manager, "images", {"paths": [str(odd)]})
            assert job.status == "done", job.error
        assert [each.data for each in host.received] == [png()]

    _with_app(test)


def test_a_fifo_is_refused_without_being_opened(world: World) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("no named pipes here")
    fifo = world.downloads / "pipe.png"
    os.mkfifo(fifo)

    async def test(client: TestClient, manager: JobManager) -> None:
        assert "regular file" in await _refused(client, "images", {"paths": [str(fifo)]})
        await _nothing_queued(client, manager)

    _with_app(test)


def test_a_file_that_changes_while_its_job_waits_is_read_again_when_it_starts(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    register_job_kind: Callable[[str, Any], None],  # noqa: F811
) -> None:
    picture = world.downloads / "cover.jpg"
    picture.write_bytes(jpeg())
    gate, run = _gate()
    register_job_kind("block", run)
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url, _events(client) as events:
            _fake_catbox(monkeypatch, url)
            blocker = (await (await _post(client, "block", {})).json())["id"]
            await _until(lambda: manager.jobs[blocker].status == "running")
            response = await _post(client, "images", {"paths": [str(picture)]})
            assert response.status == 201, await response.text()
            waiting = (await response.json())["id"]
            assert manager.jobs[waiting].status == "queued"
            # Swapped for a .torrent after the job was accepted.
            picture.write_bytes(torrent())
            gate.set()
            job = await _finished(manager, waiting)
            assert job.status == "failed"
            assert "cover.jpg" in (job.error or "")
        assert host.requests == 0
        assert PASSKEY not in " ".join(events) + str(job.detail())

    _with_app(test, max_jobs=1)


def test_a_file_outside_the_roots_is_refused_and_nothing_is_sent(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    outside = world.outside / "cover.png"
    outside.write_bytes(png())
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url:
            _fake_catbox(monkeypatch, url)
            detail = await _refused(client, "images", {"paths": [str(outside)]}, status=403)
            assert "outside download_directory and library_dirs" in detail
            # A path that climbs out is resolved first.
            climbing = world.downloads / ".." / "outside" / "cover.png"
            await _refused(client, "images", {"paths": [str(climbing)]}, status=403)
            # A relative path is not resolved against the server's working folder.
            await _refused(client, "images", {"paths": ["cover.png"]}, status=400)
            await _nothing_queued(client, manager)
        assert host.requests == 0

    _with_app(test)


def test_a_symlink_is_refused_whatever_it_leads_to(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    inside = world.downloads / "real.png"
    inside.write_bytes(png())
    outside = world.outside / "secret.png"
    outside.write_bytes(png())
    to_inside = world.downloads / "link-inside.png"
    to_inside.symlink_to(inside)
    to_outside = world.downloads / "link-outside.png"
    to_outside.symlink_to(outside)
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url:
            _fake_catbox(monkeypatch, url)
            assert "symlink" in await _refused(client, "images", {"paths": [str(to_inside)]}, status=403)
            await _refused(client, "images", {"paths": [str(to_outside)]}, status=403)
            await _nothing_queued(client, manager)
        assert host.requests == 0

    _with_app(test)


def test_a_file_in_a_folder_of_torrents_or_a_seedbox_is_refused_even_when_it_is_an_image(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    kept = world.torrents / "cover.png"
    kept.write_bytes(png())
    seeding = world.seedbox / "Album" / "cover.png"
    seeding.parent.mkdir()
    seeding.write_bytes(png())
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url:
            _fake_catbox(monkeypatch, url)
            for path in (kept, seeding):
                detail = await _refused(client, "images", {"paths": [str(path)]}, status=403)
                assert "folder of .torrent files or a seedbox's" in detail
            await _nothing_queued(client, manager)
        assert host.requests == 0

    _with_app(test)


def test_a_per_tracker_torrent_folder_is_refused_too(
    monkeypatch: pytest.MonkeyPatch, world: World, tmp_path: Path
) -> None:
    folder = world.downloads / "red-torrents"
    folder.mkdir()
    (folder / "cover.png").write_bytes(png())
    red = GazelleTrackerSettings(session="s", dottorrents_dir=str(folder))
    monkeypatch.setattr(cfg.tracker, "red", red)

    async def test(client: TestClient, manager: JobManager) -> None:
        await _refused(client, "images", {"paths": [str(folder / "cover.png")]}, status=403)
        await _nothing_queued(client, manager)

    _with_app(test)


def test_a_seedbox_folder_that_is_the_music_folder_does_not_hide_the_music(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    # The default is download_directory itself: the content check is what stands there.
    monkeypatch.setattr(cfg, "seedbox", [_seedbox(str(world.downloads))])
    picture = world.downloads / "cover.png"
    picture.write_bytes(png())
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url:
            _fake_catbox(monkeypatch, url)
            job = await _run(client, manager, "images", {"paths": [str(picture)]})
            assert job.status == "done", job.error

    _with_app(test)


def test_a_good_png_and_jpeg_are_uploaded_as_the_command_uploads_them(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    first, second = world.downloads / "front.png", world.library / "Artist" / "back.jpg"
    second.parent.mkdir()
    first.write_bytes(png())
    second.write_bytes(jpeg())
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url:
            _fake_catbox(monkeypatch, url)
            assert salmon.images.up.callback is not None
            cli = await CliRunner().invoke(salmon.images.up, [str(first), str(second)])
            assert cli.exit_code == 0, cli.output
            by_the_command = host.sent()
            host.received.clear()

            job = await _run(client, manager, "images", {"paths": [str(first), str(second)]})
            assert job.status == "done", job.error
            assert job.result["host"] == "catbox"
            assert job.result["tracker"] is None
            assert job.result["images"] == [
                {"file": "front.png", "url": "https://img.example/front.png"},
                {"file": "back.jpg", "url": "https://img.example/back.jpg"},
            ]
            # The same requests: the same fields, the same names, the same bytes.
            assert host.sent() == by_the_command
            assert [name for name, _data, _fields in by_the_command] == ["back.jpg", "front.png"]
            assert by_the_command[0][1] == jpeg()
            # What the command printed is what the job shows.
            printed = [line for line in cli.output.splitlines() if line.startswith("https://")]
            assert printed == [image["url"] for image in job.result["images"]]
            assert printed == [line["text"] for line in job.log if line["text"].startswith("https://")]

    _with_app(test)


def test_the_same_file_picked_twice_is_uploaded_once(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    picture = world.downloads / "front.png"
    picture.write_bytes(png())
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url:
            _fake_catbox(monkeypatch, url)
            job = await _run(client, manager, "images", {"paths": [str(picture), str(picture)]})
            assert job.status == "done", job.error
        assert len(host.received) == 1

    _with_app(test)


def test_an_upload_never_fills_the_servers_clipboard(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", True)
    copied: list[str] = []
    monkeypatch.setattr(salmon.images.pyperclip, "copy", copied.append)
    picture = world.downloads / "front.png"
    picture.write_bytes(png())
    host = FakeHost()

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url:
            _fake_catbox(monkeypatch, url)
            cli = await CliRunner().invoke(salmon.images.up, [str(picture)])
            assert cli.exit_code == 0, cli.output
            assert copied == ["https://img.example/front.png"]
            copied.clear()
            job = await _run(client, manager, "images", {"paths": [str(picture)]})
            assert job.status == "done", job.error
            assert copied == []

    _with_app(test)


def test_a_failed_upload_ends_the_job_with_the_hosts_reason(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    picture = world.downloads / "front.png"
    picture.write_bytes(png())

    async def test(client: TestClient, manager: JobManager) -> None:
        # Nothing listens there.
        _fake_catbox(monkeypatch, "http://127.0.0.1:9")
        job = await _run(client, manager, "images", {"paths": [str(picture)]})
        assert job.status == "failed"
        assert job.error is not None and job.error.startswith("Failed to upload image")

    _with_app(test)


def test_the_number_of_files_and_the_form_are_bounded(world: World) -> None:
    pictures = []
    for number in range(21):
        picture = world.downloads / f"{number}.png"
        picture.write_bytes(png())
        pictures.append(str(picture))

    async def test(client: TestClient, manager: JobManager) -> None:
        assert "at most 20" in await _refused(client, "images", {"paths": pictures})
        assert "at least one" in await _refused(client, "images", {"paths": []})
        response = await _post(client, "images", {"paths": pictures[:1]}, dry_run=True)
        assert response.status == 400
        assert "no dry run" in (await response.json())["detail"]
        response = await _post(client, "images", {"paths": pictures[:1], "folder": "x"})
        assert response.status == 400
        await _nothing_queued(client, manager)

    _with_app(test)


def test_a_big_image_is_refused_before_it_is_read(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    monkeypatch.setattr("salmon.images.files.MAX_IMAGE_BYTES", 100)
    big = world.downloads / "big.png"
    big.write_bytes(png() + bytes(200))

    async def test(client: TestClient, manager: JobManager) -> None:
        assert "big.png" in await _refused(client, "images", {"paths": [str(big)]})

    _with_app(test)


# --- Image hosts: the rules of salmon images up -----------------------------------------------------------------


class _Recorder:
    """An image host that sends nothing anywhere and records the files it is asked to upload."""

    def __init__(self, host: str, log: list[tuple[str, str]]) -> None:
        self.host = host
        self.log = log

    @asynccontextmanager
    async def connections(self, _limit: int) -> AsyncIterator[None]:
        yield

    async def upload_file(self, filename: str) -> tuple[str, None]:
        self.log.append((self.host, os.path.basename(filename)))
        return f"https://{self.host}.invalid/{os.path.basename(filename)}", None


@pytest.fixture
def hosts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every image host a recorder; the (host, file) of each upload. The trackers are RED, OPS and DIC."""
    log: list[tuple[str, str]] = []
    for name in list(salmon.images.HOSTS):
        monkeypatch.setitem(
            salmon.images.HOSTS, name, SimpleNamespace(ImageUploader=lambda name=name: _Recorder(name, log))
        )
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])
    return log


def _config(monkeypatch: pytest.MonkeyPatch, **settings: Any) -> None:
    monkeypatch.setattr(cfg, "image", msgspec.convert(settings, ImageUploader))


def test_red_own_host_is_for_red_and_ops_and_only_with_a_tracker_named(
    monkeypatch: pytest.MonkeyPatch, world: World, hosts: list[tuple[str, str]]
) -> None:
    _config(monkeypatch, image_uploader="catbox")
    picture = world.downloads / "front.png"
    picture.write_bytes(png())
    params = {"paths": [str(picture)]}

    async def test(client: TestClient, manager: JobManager) -> None:
        # Nothing says whose pages the images are for.
        detail = await _refused(client, "images", {**params, "host": "red"})
        assert "red can't be used without a tracker" in detail
        assert "only display on RED/OPS" in detail
        # Where they display.
        for tracker in ("RED", "ops"):
            job = await _run(client, manager, "images", {**params, "host": "red", "tracker": tracker})
            assert job.status == "done", job.error
            assert job.result["host"] == "red"
            assert job.result["tracker"] == tracker.upper()
        # Where they do not, as the command says it.
        detail = await _refused(client, "images", {**params, "host": "red", "tracker": "DIC"})
        assert detail == "red can't be used for DIC's images: its images only display on RED/OPS"
        assert hosts == [("red", "front.png")] * 2

    _with_app(test)


def test_a_tracker_decides_the_host_unless_one_is_named(
    monkeypatch: pytest.MonkeyPatch, world: World, hosts: list[tuple[str, str]]
) -> None:
    _config(monkeypatch, image_uploader="catbox", ops={"image_uploader": "imgbox"}, red={"image_uploader": "red"})
    picture = world.downloads / "front.png"
    picture.write_bytes(png())
    params = {"paths": [str(picture)]}

    async def test(client: TestClient, manager: JobManager) -> None:
        for extra in (
            {},
            {"tracker": "OPS"},
            {"tracker": "RED"},
            {"tracker": "DIC"},
            {"tracker": "OPS", "host": "catbox"},
        ):
            job = await _run(client, manager, "images", {**params, **extra})
            assert job.status == "done", job.error
        assert [host for host, _ in hosts] == ["catbox", "imgbox", "red", "catbox", "catbox"]

    _with_app(test)


def test_an_unknown_host_or_tracker_is_refused(
    monkeypatch: pytest.MonkeyPatch, world: World, hosts: list[tuple[str, str]]
) -> None:
    picture = world.downloads / "front.png"
    picture.write_bytes(png())
    params = {"paths": [str(picture)]}

    async def test(client: TestClient, manager: JobManager) -> None:
        assert "not a valid image host" in await _refused(client, "images", {**params, "host": "nowhere"})
        assert "not a tracker in your config" in await _refused(client, "images", {**params, "tracker": "XYZ"})
        await _nothing_queued(client, manager)
        assert hosts == []

    _with_app(test)


# --- The folder browser lists image files ----------------------------------------------------------------------


def test_the_browser_lists_image_files_only_where_it_may_and_bounded(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    folder = world.downloads / "Album"
    folder.mkdir()
    (folder / "Cover.PNG").write_bytes(png())
    (folder / "back.jpg").write_bytes(jpeg())
    (folder / "01.flac").write_bytes(b"x")
    (folder / "rip.log").write_bytes(b"x")
    (folder / ".hidden.png").write_bytes(png())
    (folder / "link.png").symlink_to(folder / "back.jpg")
    (world.torrents / "kept.png").write_bytes(png())

    async def test(client: TestClient, manager: JobManager) -> None:
        listed = await (
            await client.get("/api/browse", params={"path": str(folder), "images": "1"}, headers=AUTH)
        ).json()
        assert [(each["name"], each["path"], each["size"]) for each in listed["images"]] == [
            ("back.jpg", str(folder / "back.jpg"), len(jpeg())),
            ("Cover.PNG", str(folder / "Cover.PNG"), len(png())),
        ]
        assert listed["audio"] is True
        # Without asking, the listing is what it was.
        plain = await (await client.get("/api/browse", params={"path": str(folder)}, headers=AUTH)).json()
        assert "images" not in plain
        # Nothing is offered from a folder of torrents.
        kept = await (
            await client.get("/api/browse", params={"path": str(world.torrents), "images": "1"}, headers=AUTH)
        ).json()
        assert kept["images"] == []
        roots = await (await client.get("/api/browse", params={"images": "1"}, headers=AUTH)).json()
        assert roots["images"] == []
        # Outside the roots, nothing at all.
        response = await client.get("/api/browse", params={"path": str(world.outside), "images": "1"}, headers=AUTH)
        assert response.status == 403

        monkeypatch.setattr(paths, "MAX_LISTED", 1)
        cut = await (await client.get("/api/browse", params={"path": str(folder), "images": "1"}, headers=AUTH)).json()
        assert len(cut["images"]) == 1
        assert cut["truncated"] is True

    _with_app(test)


# --- Description generator ---------------------------------------------------------------------------------------


def _store_run(
    test: Callable[[TestClient, JobManager, FakeStore], Awaitable[None]], store: FakeStore | None = None
) -> FakeStore:
    store = store or FakeStore()

    async def serving(client: TestClient, manager: JobManager) -> None:
        async with store.serving():
            await test(client, manager, store)

    _with_app(serving)
    return store


def test_the_description_is_the_one_the_command_makes_for_the_same_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)
    copied: list[str] = []
    monkeypatch.setattr(salmon.commands.pyperclip, "copy", copied.append)
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", True)

    async def test(client: TestClient, manager: JobManager, store: FakeStore) -> None:
        urls = [store.url("/album/x"), store.url("/album/y")]
        cli = await CliRunner().invoke(salmon.commands.descgen, urls)
        assert cli.exit_code == 0, cli.output
        # The command prints it and copies it.
        (description,) = copied
        assert cli.output.endswith(f"\nDescription:\n\n{description}\n")
        assert "[b][size=4]Tracklist[/size][/b]" in description
        asked = list(store.requests)
        store.requests.clear()
        copied.clear()

        job = await _run(client, manager, "descgen", {"urls": urls})
        assert job.status == "done", job.error
        assert job.result == {"description": description}
        # The same pages were fetched, once each, and the server's clipboard was left alone.
        assert sorted(store.requests) == sorted(asked) == ["/album/x", "/album/y"]
        assert copied == []

    _store_run(test)


def test_a_url_no_scraper_takes_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)

    async def test(client: TestClient, manager: JobManager, store: FakeStore) -> None:
        bad = "https://shop.example.com/releases/123"
        detail = await _refused(client, "descgen", {"urls": [store.url("/album/x"), bad]})
        # The command's own message, naming the URL and the sources.
        assert f"{bad} is not a release URL descgen supports. It takes URLs from " in detail
        assert all(name in detail for name in METASOURCES)
        # A folder is no URL, and nothing here says whether it exists: that is the command's, on its own machine.
        folder = str(world.downloads)
        detail = await _refused(client, "descgen", {"urls": [folder]})
        assert f"{folder} is not a release URL descgen supports" in detail
        assert "is a folder" not in detail
        assert "at least one" in await _refused(client, "descgen", {"urls": []})
        await _nothing_queued(client, manager)
        assert store.requests == []

    _store_run(test)


def test_eleven_urls_are_refused_and_ten_are_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)

    async def test(client: TestClient, manager: JobManager, store: FakeStore) -> None:
        urls = [store.url(f"/album/{number}") for number in range(11)]
        assert "at most 10" in await _refused(client, "descgen", {"urls": urls})
        assert store.requests == []
        job = await _run(client, manager, "descgen", {"urls": urls[:10]})
        assert job.status == "done", job.error
        assert sorted(store.requests) == sorted(f"/album/{number}" for number in range(10))

    _store_run(test)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_a_url_to_an_internal_address_is_refused_by_the_guard(host: str) -> None:
    async def test(client: TestClient, manager: JobManager, store: FakeStore) -> None:
        job = await _run(client, manager, "descgen", {"urls": [store.url("/album/x", host)]})
        assert job.status == "failed"
        assert job.result is None
        assert store.requests == []

    _store_run(test)


def test_a_failing_store_ends_the_job_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)

    async def test(client: TestClient, manager: JobManager, store: FakeStore) -> None:
        store.page = "<html><body>not an album</body></html>"
        job = await _run(client, manager, "descgen", {"urls": [store.url("/album/x")]})
        assert job.status == "failed"
        assert job.error is not None and job.error.startswith("Scrape failed")

    _store_run(test)


PLANTED_STORE = {
    "qobuz": "planted-qobuz-user-token-5b1e",
    "tidal": "planted-tidal-token-8e42",
    "discogs": "planted-discogs-token-a9d0",
}


async def _everything(client: TestClient, events: list[str], job: jobs.Job) -> str:
    """All that left the server about a job: its events, its record and the job list."""
    listed = await (await client.get("/api/jobs", headers=AUTH)).text()
    return "\n".join([*events, str(job.detail()), listed])


def test_no_store_secret_leaves_in_a_description(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", PLANTED_STORE["qobuz"])
    monkeypatch.setattr(cfg.metadata.tidal, "token", PLANTED_STORE["tidal"])
    monkeypatch.setattr(cfg.metadata, "discogs_token", PLANTED_STORE["discogs"])
    secrets = " ".join(PLANTED_STORE.values())
    store = FakeStore(bandcamp_page().replace("The Album", secrets))

    async def test(client: TestClient, manager: JobManager, store: FakeStore) -> None:
        async with _events(client) as events:
            url = store.url(f"/album/x?k={PLANTED_STORE['qobuz']}")
            job = await _run(client, manager, "descgen", {"urls": [url]})
            assert job.status == "done", job.error
            assert "[REDACTED]" in job.result["description"]
            everything = await _everything(client, events, job)
        for name, secret in PLANTED_STORE.items():
            assert secret not in everything, name

    _store_run(test, store)


# --- Planted image host keys ---------------------------------------------------------------------------------------


def test_no_image_host_key_leaves_in_an_upload(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    for name, secret in PLANTED_KEYS.items():
        monkeypatch.setattr(cfg.image, name, secret)
    picture = world.downloads / "front.png"
    picture.write_bytes(png())
    # The host repeats every key in the URL it answers with.
    host = FakeHost(echo="".join(f"/{secret}" for secret in PLANTED_KEYS.values()))

    async def test(client: TestClient, manager: JobManager) -> None:
        async with host.serving() as url, _events(client) as events:
            _fake_catbox(monkeypatch, url)
            job = await _run(client, manager, "images", {"paths": [str(picture)]})
            assert job.status == "done", job.error
            everything = await _everything(client, events, job)
        assert "[REDACTED]" in everything
        for name, secret in PLANTED_KEYS.items():
            assert secret not in everything, name

    _with_app(test)


# --- Spectrals upload ----------------------------------------------------------------------------------------

TRACKS = {"01": "01 Track.flac", "02": "02 Track.flac"}


@pytest.fixture
def spectrals_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spectrals job that writes real PNGs into its own folder, as the real one does, and shows them."""

    async def run(params: Any) -> dict[str, Any]:
        folder = jobs.own_folder("spectrals")
        for number in TRACKS:
            for part in ("Full", "Zoom"):
                Path(folder, f"{number} {part}.png").write_bytes(png(f"#{number}0000"))
        await interaction.show_spectrals(folder, {int(number): name for number, name in TRACKS.items()})
        return {"folder": "Album", "tracks": TRACKS, "marks_found": False}

    original = jobs.KINDS["spectrals"]
    stand_in = dataclasses.replace(
        original, run=run, folder=lambda _params: None, check=lambda params, _dry_run: params
    )
    monkeypatch.setitem(jobs.KINDS, "spectrals", stand_in)


async def _spectrals(client: TestClient, manager: JobManager, answer: bool = True) -> jobs.Job:
    """A spectrals job showing its images, and finished when `answer`."""
    response = await _post(client, "spectrals", {"path": "/x"})
    assert response.status == 201, await response.text()
    job_id = (await response.json())["id"]
    assert (await _question(manager, job_id))["kind"] == "spectrals"
    if answer:
        await _answer(client, manager, job_id, True)
        job = await _finished(manager, job_id)
        assert job.status == "done", job.error
    return manager.jobs[job_id]


def _urls(host: str, number: int) -> list[str]:
    return [f"https://{host}.invalid/{number:02d} Full.png", f"https://{host}.invalid/{number:02d} Zoom.png"]


def test_the_spectrals_go_to_the_spectrals_host_of_the_tracker_picked(
    monkeypatch: pytest.MonkeyPatch, world: World, spectrals_job: None, hosts: list[tuple[str, str]]
) -> None:
    _config(
        monkeypatch,
        specs_uploader="catbox",
        oeimg_key="k",
        ops={"specs_uploader": "imgbox"},
        dic={"specs_uploader": "oeimg"},
    )

    async def test(client: TestClient, manager: JobManager) -> None:
        source = await _spectrals(client, manager)
        (folder,) = jobs.own_folders(source)
        names = sorted(os.listdir(folder))
        assert names == ["01 Full.png", "01 Zoom.png", "02 Full.png", "02 Zoom.png"]

        for tracker, host in (("OPS", "imgbox"), ("red", "catbox"), (None, "catbox"), ("DIC", "oeimg")):
            hosts.clear()
            job = await _run(client, manager, "spectrals_upload", {"job_id": source.id, "tracker": tracker})
            assert job.status == "done", job.error
            result = job.result
            assert (result["host"], result["tracker"]) == (host, tracker.upper() if tracker else None)
            # From the job's own folder only, and every image of every track.
            assert sorted(hosts) == sorted((host, name) for name in names)
            assert result["tracks"] == [
                {"id": number, "track": TRACKS[f"{number:02d}"], "urls": _urls(host, number)} for number in (1, 2)
            ]
            # What salmon specs prints, plain and with -f.
            assert result["text"] == "\n".join(
                f"{TRACKS[f'{number:02d}']}: {' '.join(_urls(host, number))}" for number in (1, 2)
            )
            first = _urls(host, 1)
            assert result["bbcode"].splitlines()[0] == f"[hide=01 Track.flac][img={first[0]}][img={first[1]}][/hide]"
        # The images are still the job's: the upload took none away.
        assert sorted(os.listdir(folder)) == names

    _with_app(test)


def test_a_spectrals_host_the_rules_refuse_is_refused(
    monkeypatch: pytest.MonkeyPatch, world: World, spectrals_job: None, hosts: list[tuple[str, str]]
) -> None:
    _config(monkeypatch, specs_uploader="catbox", ops={"specs_uploader": "imgbox"})

    async def test(client: TestClient, manager: JobManager) -> None:
        source = await _spectrals(client, manager)
        # The config does not load with these; a request is not to rely on that.
        monkeypatch.setattr(cfg.image, "specs_uploader", "ra")
        detail = await _refused(client, "spectrals_upload", {"job_id": source.id})
        assert detail == "ra can't be used for spectrals: Ra's owner asks not to use it for spectrals."
        monkeypatch.setattr(cfg.image.ops, "specs_uploader", "red")
        detail = await _refused(client, "spectrals_upload", {"job_id": source.id, "tracker": "OPS"})
        assert detail.startswith("red can't be used for spectrals: RED's rules forbid spectrals on its image host")
        detail = await _refused(client, "spectrals_upload", {"job_id": source.id, "tracker": "XYZ"})
        assert "not a tracker in your config" in detail
        assert hosts == []

    _with_app(test)


def test_the_spectrals_of_a_discarded_or_unfinished_job_are_not_uploaded(
    monkeypatch: pytest.MonkeyPatch, world: World, spectrals_job: None, hosts: list[tuple[str, str]]
) -> None:
    _config(monkeypatch, specs_uploader="catbox")

    async def test(client: TestClient, manager: JobManager) -> None:
        shown = await _spectrals(client, manager, answer=False)
        assert "has not finished" in await _refused(client, "spectrals_upload", {"job_id": shown.id}, status=409)
        await _answer(client, manager, shown.id, True)
        await _finished(manager, shown.id)

        discarded = await _spectrals(client, manager)
        response = await client.post(f"/api/jobs/{discarded.id}/discard", json={}, headers=AUTH)
        assert response.status == 200
        assert "were discarded" in await _refused(client, "spectrals_upload", {"job_id": discarded.id}, status=409)

        detail = await _refused(client, "spectrals_upload", {"job_id": "job-999"}, status=404)
        assert "No such spectrals job" in detail
        assert hosts == []

    _with_app(test)


def test_a_job_that_is_not_a_spectrals_job_gives_no_spectrals(
    spectrals_job: None,
    hosts: list[tuple[str, str]],
    register_job_kind: Callable[[str, Any], None],  # noqa: F811
) -> None:
    async def run(_params: Params) -> str:
        return "x"

    register_job_kind("other", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        other = (await (await _post(client, "other", {})).json())["id"]
        await _finished(manager, other)
        assert "No such spectrals job" in await _refused(client, "spectrals_upload", {"job_id": other}, status=404)
        assert hosts == []

    _with_app(test)


def test_a_cancelled_spectrals_job_gives_no_spectrals(spectrals_job: None, hosts: list[tuple[str, str]]) -> None:
    async def test(client: TestClient, manager: JobManager) -> None:
        shown = await _spectrals(client, manager, answer=False)
        response = await client.post(f"/api/jobs/{shown.id}/cancel", json={}, headers=AUTH)
        assert response.status == 200
        await _finished(manager, shown.id)
        await _refused(client, "spectrals_upload", {"job_id": shown.id}, status=409)
        assert hosts == []

    _with_app(test)


def test_a_spectral_that_is_no_longer_an_image_is_not_uploaded(
    monkeypatch: pytest.MonkeyPatch, spectrals_job: None, hosts: list[tuple[str, str]]
) -> None:
    _config(monkeypatch, specs_uploader="catbox")

    async def test(client: TestClient, manager: JobManager) -> None:
        source = await _spectrals(client, manager)
        (folder,) = jobs.own_folders(source)
        # Swapped, in the folder of the job, for a .torrent.
        Path(folder, "02 Zoom.png").write_bytes(torrent())
        job = await _run(client, manager, "spectrals_upload", {"job_id": source.id})
        assert job.status == "failed"
        assert job.error is not None and "02 Zoom.png is not an image any more" in job.error
        assert hosts == []
        assert PASSKEY not in str(job.detail())

    _with_app(test)


def test_a_spectral_replaced_by_a_symlink_is_not_uploaded(
    monkeypatch: pytest.MonkeyPatch, world: World, spectrals_job: None, hosts: list[tuple[str, str]]
) -> None:
    _config(monkeypatch, specs_uploader="catbox")
    elsewhere = world.outside / "picture.png"
    elsewhere.write_bytes(png())

    async def test(client: TestClient, manager: JobManager) -> None:
        source = await _spectrals(client, manager)
        (folder,) = jobs.own_folders(source)
        os.remove(Path(folder, "01 Full.png"))
        Path(folder, "01 Full.png").symlink_to(elsewhere)
        job = await _run(client, manager, "spectrals_upload", {"job_id": source.id})
        assert job.status == "failed"
        assert job.error is not None and "01 Full.png" in job.error
        assert hosts == []

    _with_app(test)


def test_a_dry_run_of_a_spectrals_upload_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, spectrals_job: None, hosts: list[tuple[str, str]]
) -> None:
    _config(monkeypatch, specs_uploader="catbox")

    async def test(client: TestClient, manager: JobManager) -> None:
        source = await _spectrals(client, manager)
        job = await _run(client, manager, "spectrals_upload", {"job_id": source.id}, dry_run=True)
        assert job.status == "done", job.error
        assert hosts == []
        assert all(url.startswith("<catbox URL of ") for track in job.result["tracks"] for url in track["urls"])

    _with_app(test)


# --- What the Tools page offers -----------------------------------------------------------------------------


def test_the_tools_options_say_which_host_each_tracker_uses(
    monkeypatch: pytest.MonkeyPatch, hosts: list[tuple[str, str]]
) -> None:
    _config(monkeypatch, image_uploader="catbox", ops={"image_uploader": "imgbox", "specs_uploader": "imgbox"})

    async def test(client: TestClient, manager: JobManager) -> None:
        assert (await client.get("/api/tools/options")).status == 401
        answer = await (await client.get("/api/tools/options", headers=AUTH)).json()
        by_name = {host["name"]: host["only_for"] for host in answer["image_hosts"]}
        assert by_name["red"] == ["RED", "OPS"]
        assert by_name["catbox"] == []
        assert answer["trackers"] == [
            {"code": "RED", "image_host": "catbox", "spectrals_host": "catbox"},
            {"code": "OPS", "image_host": "imgbox", "spectrals_host": "imgbox"},
            {"code": "DIC", "image_host": "catbox", "spectrals_host": "catbox"},
        ]
        assert (answer["image_host"], answer["spectrals_host"]) == ("catbox", "catbox")
        assert (answer["max_images"], answer["max_descgen_urls"]) == (20, 10)

    _with_app(test)
