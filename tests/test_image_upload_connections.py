"""Batch uploads to an image host share a few reused connections (#475).

The real catbox and ptscreens uploaders run against a local fake host that counts the
connections made to it. Nothing here contacts a real image host.
"""

import asyncio
import contextlib
import gc
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import aiohttp
import anyio
import pytest
from aiohttp import web
from aiohttp.web_protocol import RequestHandler

import salmon.images as images
import salmon.images.base as images_base
from salmon.errors import ImageUploadFailed

REAL_HOSTS = ("https://catbox.moe", "https://ptscreens.com")


class FakeHost:
    """A local image host that answers as catbox and ptscreens do, and counts its connections."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self.opened = 0
        self.open = 0
        self.peak = 0
        self.delay = 0.0
        # Filename -> (seconds to wait, then fail with a 500).
        self.failing: dict[str, float] = {}
        self._stopping = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._server: asyncio.Server | None = None

    async def start(self) -> str:
        app = web.Application(client_max_size=16 * 1024**2)
        app.router.add_post("/{tail:.*}", self._upload)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        host = self

        class CountingHandler(RequestHandler):
            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                host.opened += 1
                host.open += 1
                host.peak = max(host.peak, host.open)
                super().connection_made(transport)

            def connection_lost(self, exc: BaseException | None) -> None:
                host.open -= 1
                super().connection_lost(exc)

        server = self._runner.server
        assert server is not None
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(lambda: CountingHandler(server, loop=loop), "127.0.0.1", 0)
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.close()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _sleep(self, seconds: float) -> None:
        # Cut short when the host stops, so shutting it down does not wait for the sleep.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)

    async def _upload(self, request: web.Request) -> web.Response:
        form = await request.post()
        (filename,) = [unquote(field.filename) for field in form.values() if isinstance(field, web.FileField)]
        self.received.append(filename)
        if filename in self.failing:
            await self._sleep(self.failing[filename])
            return web.Response(status=500, text="broken")
        await self._sleep(self.delay)
        url = f"https://img.example/{request.path.strip('/')}/{filename}"
        if request.path == "/user/api.php":
            return web.Response(text=url)
        return web.json_response({"image": {"url": url}})


def _redirect(monkeypatch: pytest.MonkeyPatch, targets: dict[str, str]) -> None:
    """Send the uploaders' requests for the real hosts to the local ones instead."""
    real_post = aiohttp.ClientSession.post

    def local_post(self: aiohttp.ClientSession, url: str, *args: Any, **kwargs: Any) -> Any:
        for real, local in targets.items():
            if url.startswith(real):
                return real_post(self, url.replace(real, local, 1), *args, **kwargs)
        raise AssertionError(f"request to a host that is not faked: {url}")

    monkeypatch.setattr(aiohttp.ClientSession, "post", local_post)


def _spectrals(tmp_path: Path, tracks: int) -> list[tuple[int, str, tuple[str, str]]]:
    spectrals = []
    for track in range(1, tracks + 1):
        paths = []
        for kind in ("Full", "Zoom"):
            path = tmp_path / f"{track:02d} {kind}.png"
            path.write_bytes(b"png" * 1000)
            paths.append(str(path))
        spectrals.append((track, f"track{track}.flac", (paths[0], paths[1])))
    return spectrals


def _names(spectrals: list[tuple[int, str, tuple[str, str]]], tracks: range) -> list[str]:
    return sorted(Path(path).name for sid, _, paths in spectrals if sid in tracks for path in paths)


def _run(monkeypatch: pytest.MonkeyPatch, body: Callable[[FakeHost, FakeHost], Awaitable[None]]) -> None:
    """Run a test against two fake hosts (as catbox and ptscreens), then fail on anything left unclosed."""
    unclosed: list[str] = []
    monkeypatch.setattr(images.click, "secho", lambda *args, **kwargs: None)

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        catbox, ptscreens = FakeHost(), FakeHost()
        _redirect(monkeypatch, dict(zip(REAL_HOSTS, [await catbox.start(), await ptscreens.start()], strict=True)))
        try:
            await body(catbox, ptscreens)
        finally:
            await catbox.stop()
            await ptscreens.stop()
        # An unclosed session or connector is only reported once it is collected.
        gc.collect()
        await anyio.sleep(0.05)

    anyio.run(main)
    assert unclosed == []


def test_spectrals_share_a_few_connections_and_reuse_them(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 3, raising=False)
    spectrals = _spectrals(tmp_path, tracks=10)

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.delay = 0.05
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])

        assert sorted(result) == list(range(1, 11))
        assert result[4] == [f"https://img.example/user/api.php/04 {kind}.png" for kind in ("Full", "Zoom")]
        # Every image uploaded exactly once.
        assert sorted(catbox.received) == _names(spectrals, range(1, 11))
        assert max(Counter(catbox.received).values()) == 1
        # At most 3 connections, never more than 3 open at once, reused across the 20 images.
        assert catbox.peak <= 3
        assert catbox.opened <= 3

    _run(monkeypatch, body)


def test_a_failure_stops_new_tracks_and_the_rest_go_to_the_retry_host_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    spectrals = _spectrals(tmp_path, tracks=10)
    prompts: list[str] = []

    async def prompt(message: str, **kwargs: object) -> str:
        prompts.append(message)
        return "ptscreens"

    monkeypatch.setattr(images.click, "prompt", prompt)

    async def body(catbox: FakeHost, ptscreens: FakeHost) -> None:
        catbox.delay = 0.1
        catbox.failing = {"01 Full.png": 0.0}
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])

        assert len(prompts) == 1
        # Track 1 was started, so both its images went; no other track was sent to the failing host.
        assert sorted(catbox.received) == _names(spectrals, range(1, 2))
        # Everything not uploaded went to the host picked at the prompt, once.
        assert sorted(ptscreens.received) == _names(spectrals, range(1, 11))
        assert sorted(result) == list(range(1, 11))
        assert all(url.startswith("https://img.example/api/1/upload/") for urls in result.values() for url in urls)

    _run(monkeypatch, body)


def test_a_slow_failure_does_not_hold_up_the_rest_of_the_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    spectrals = _spectrals(tmp_path, tracks=10)

    async def prompt(message: str, **kwargs: object) -> str:
        return "ptscreens"

    monkeypatch.setattr(images.click, "prompt", prompt)

    async def body(catbox: FakeHost, ptscreens: FakeHost) -> None:
        catbox.delay = 0.01
        # Track 2's Full image fails, but only after every other image had time to go.
        catbox.failing = {"02 Full.png": 1.0}
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])

        # The other connection kept taking images from the queue while track 2 was stuck.
        assert sorted(catbox.received) == _names(spectrals, range(1, 11))
        # Only track 2 is left for the retry host, and nothing is sent twice to either host.
        assert sorted(ptscreens.received) == _names(spectrals, range(2, 3))
        assert max(Counter(catbox.received).values()) == 1
        assert sorted(result) == list(range(1, 11))

    _run(monkeypatch, body)


def test_waiting_in_the_queue_does_not_count_against_the_upload_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    # Each upload takes 0.2 s, well within 1 s, but the last ones wait about 1.6 s for a connection.
    monkeypatch.setattr(images_base, "UPLOAD_TIMEOUT", aiohttp.ClientTimeout(total=1, sock_connect=1), raising=False)
    spectrals = _spectrals(tmp_path, tracks=8)

    async def prompt(message: str, **kwargs: object) -> str:
        raise AssertionError("an upload failed")

    monkeypatch.setattr(images.click, "prompt", prompt)

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.delay = 0.2
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])
        assert sorted(result) == list(range(1, 9))

    _run(monkeypatch, body)


def test_cancelling_a_batch_closes_its_connections(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    spectrals = _spectrals(tmp_path, tracks=4)

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.delay = 10
        with anyio.move_on_after(0.3):
            await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])
        assert len(catbox.received) == 2
        await anyio.sleep(0.1)
        assert catbox.open == 0

    _run(monkeypatch, body)


def test_images_up_shares_the_same_few_connections(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    paths = []
    for index in range(6):
        path = tmp_path / f"image{index}.png"
        path.write_bytes(b"png")
        paths.append(str(path))

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.delay = 0.05
        urls = await images.upload_images(tuple(paths), images.HOSTS["catbox"])

        assert urls == [f"https://img.example/user/api.php/image{index}.png" for index in range(6)]
        assert sorted(catbox.received) == sorted(Path(path).name for path in paths)
        assert catbox.peak <= 2
        assert catbox.opened <= 2

    _run(monkeypatch, body)


def test_images_up_still_fails_when_an_upload_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    path = tmp_path / "image.png"
    path.write_bytes(b"png")

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.failing = {"image.png": 0.0}
        with pytest.raises(ImageUploadFailed):
            await images.upload_images((str(path),), images.HOSTS["catbox"])

    _run(monkeypatch, body)
