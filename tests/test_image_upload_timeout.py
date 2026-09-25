"""A timed-out image upload must not crash the upload (follow-up to #428).

aiohttp raises a plain TimeoutError, not aiohttp.ClientError, when UPLOAD_TIMEOUT's total runs
out. Every image host module except imgbox.py (which already catches everything) and red.py
(which goes through RedApi._request, already turning timeouts into RequestError) needs to catch
it too. Nothing here contacts a real image host: all requests are redirected to a local fake one.
"""

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import aiohttp
import anyio
import pytest
from aiohttp import web

import salmon.images as images
import salmon.images.base as images_base
from salmon.errors import ImageUploadFailed
from salmon.images import catbox, imgbb, oeimg, ptscreens, ra

REAL_HOSTS = ("https://catbox.moe", "https://ptscreens.com")

# The five modules the brief asks to fix, and the host each one talks to.
TIMEOUT_MODULES = {
    "catbox": (catbox, "https://catbox.moe"),
    "imgbb": (imgbb, "https://api.imgbb.com"),
    "oeimg": (oeimg, "https://imgoe.download"),
    "ptscreens": (ptscreens, "https://ptscreens.com"),
    "ra": (ra, "https://thesungod.xyz"),
}

TINY_TIMEOUT = aiohttp.ClientTimeout(total=0.1, sock_connect=0.1)


class FakeHost:
    """A local image host that answers like catbox/ptscreens, but never replies for a hanging file."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self.hanging: set[str] = set()
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        app = web.Application(client_max_size=16 * 1024**2)
        app.router.add_route("*", "/{tail:.*}", self._upload)
        # A short shutdown_timeout: a hanging handler's own sleep is capped low enough (below)
        # that waiting for it is fine, and this keeps a slow shutdown from stalling the test.
        self._runner = web.AppRunner(app, shutdown_timeout=1)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        assert self._runner.addresses
        port = self._runner.addresses[0][1]
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _upload(self, request: web.Request) -> web.Response:
        form = await request.post()
        (filename,) = [unquote(field.filename) for field in form.values() if isinstance(field, web.FileField)]
        self.received.append(filename)
        if filename in self.hanging:
            # Received, but not answered until well after the client's tiny timeout fires.
            await asyncio.sleep(2)
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


@pytest.mark.parametrize("name", sorted(TIMEOUT_MODULES))
def test_a_timeout_becomes_image_upload_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    module, real_host = TIMEOUT_MODULES[name]
    monkeypatch.setattr(images_base, "UPLOAD_TIMEOUT", TINY_TIMEOUT, raising=False)
    path = tmp_path / "image.png"
    path.write_bytes(b"png")

    async def run() -> None:
        host = FakeHost()
        local = await host.start()
        host.hanging = {"image.png"}
        _redirect(monkeypatch, {real_host: local})
        try:
            with pytest.raises(ImageUploadFailed, match="(?i)timed out"):
                await module.ImageUploader().upload_file(str(path))
        finally:
            await host.stop()

    anyio.run(run)


def test_a_timed_out_spectral_reaches_the_retry_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images.click, "secho", lambda *args, **kwargs: None)
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 3, raising=False)
    monkeypatch.setattr(images_base, "UPLOAD_TIMEOUT", TINY_TIMEOUT, raising=False)
    spectrals = _spectrals(tmp_path, tracks=3)
    prompts: list[str] = []

    async def prompt(message: str, **kwargs: object) -> str:
        prompts.append(message)
        return "ptscreens"

    monkeypatch.setattr(images.click, "prompt", prompt)

    async def body() -> None:
        catbox_host, ptscreens_host = FakeHost(), FakeHost()
        _redirect(
            monkeypatch,
            dict(zip(REAL_HOSTS, [await catbox_host.start(), await ptscreens_host.start()], strict=True)),
        )
        catbox_host.hanging = {"02 Full.png"}
        try:
            result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])
            # The stuck track's images went to the retry host, asked for once.
            assert len(prompts) == 1
            assert sorted(ptscreens_host.received) == _names(spectrals, range(2, 3))
            # The other tracks uploaded to catbox and were not retried anywhere.
            assert "01 Full.png" in catbox_host.received
            assert "03 Full.png" in catbox_host.received
            assert "01 Full.png" not in ptscreens_host.received
            assert "03 Full.png" not in ptscreens_host.received
            assert sorted(result) == [1, 2, 3]
        finally:
            await catbox_host.stop()
            await ptscreens_host.stop()

    anyio.run(body)


def test_upload_cover_returns_none_and_prints_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(images_base, "UPLOAD_TIMEOUT", TINY_TIMEOUT, raising=False)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpg")

    async def run() -> str | None:
        host = FakeHost()
        local = await host.start()
        host.hanging = {"cover.jpg"}
        _redirect(monkeypatch, {"https://catbox.moe": local})
        try:
            return await images.upload_cover(str(cover), "catbox")
        finally:
            await host.stop()

    result = anyio.run(run)
    assert result is None
    assert "failed" in capsys.readouterr().out
