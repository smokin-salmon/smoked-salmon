"""Helpers of tests/test_image_upload_connections.py that other test modules use too."""

import asyncio
import contextlib
import gc
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
