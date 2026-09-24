"""Ra (thesungod.xyz) image host uploader.

Tests talk only to a local aiohttp fake server, monkeypatching ra.UPLOAD_URL so the
uploader's own aiohttp.ClientSession never reaches the real host.
"""

import anyio
import pytest
from aiohttp import web

from salmon.errors import ImageUploadFailed
from salmon.images import ra


async def _serve(handler) -> web.AppRunner:
    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


def _local_url(runner: web.AppRunner) -> str:
    return f"http://127.0.0.1:{runner.addresses[0][1]}"


async def _run_upload(tmp_path) -> tuple[str, None]:
    image_path = tmp_path / "cover.jpg"
    image_path.write_bytes(b"fake-image-bytes")
    uploader = ra.ImageUploader()
    return await uploader.upload_file(str(image_path))


def test_ra_upload_sends_api_key_and_image_fields(tmp_path, monkeypatch) -> None:
    received: dict[str, object] = {}

    async def handle_upload(request: web.Request) -> web.Response:
        data = await request.post()
        received["api_key"] = data.get("api_key")
        received["image"] = data.get("image")
        return web.json_response({"links": ["https://ra.thesungod.xyz/abc.jpg"]})

    async def run() -> None:
        runner = await _serve(handle_upload)
        monkeypatch.setattr(ra, "UPLOAD_URL", _local_url(runner))
        monkeypatch.setattr(ra.cfg.image, "ra_key", "test-ra-key")
        try:
            url, deletion_url = await _run_upload(tmp_path)
            assert url == "https://ra.thesungod.xyz/abc.jpg"
            assert deletion_url is None
        finally:
            await runner.cleanup()

    anyio.run(run)
    assert received["api_key"] == "test-ra-key"
    assert received["image"] is not None


def test_ra_error_response_surfaces_the_status_and_body(tmp_path, monkeypatch) -> None:
    async def handle_upload(request: web.Request) -> web.Response:
        return web.json_response({"error": "invalid api key"}, status=500)

    async def run() -> None:
        runner = await _serve(handle_upload)
        monkeypatch.setattr(ra, "UPLOAD_URL", _local_url(runner))
        try:
            with pytest.raises(ImageUploadFailed, match="500.*invalid api key"):
                await _run_upload(tmp_path)
        finally:
            await runner.cleanup()

    anyio.run(run)


def test_ra_success_without_links_raises(tmp_path, monkeypatch) -> None:
    async def handle_upload(request: web.Request) -> web.Response:
        return web.json_response({})

    async def run() -> None:
        runner = await _serve(handle_upload)
        monkeypatch.setattr(ra, "UPLOAD_URL", _local_url(runner))
        try:
            with pytest.raises(ImageUploadFailed):
                await _run_upload(tmp_path)
        finally:
            await runner.cleanup()

    anyio.run(run)
