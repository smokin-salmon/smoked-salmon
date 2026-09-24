import aiohttp
import anyio
import pytest
from aiohttp import web

from salmon.errors import ImageUploadFailed
from salmon.images import imgbb, ptscreens

# Never contact the real hosts: redirect their hardcoded URLs to a local fake server instead.
REAL_HOSTS = {
    "imgbb": "https://api.imgbb.com",
    "ptscreens": "https://ptscreens.com",
}


async def _serve(handler) -> web.AppRunner:
    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


def _local_url(runner: web.AppRunner) -> str:
    return f"http://127.0.0.1:{runner.addresses[0][1]}"


def _redirect_to_local(monkeypatch, host_key: str, local_base: str) -> None:
    real_post = aiohttp.ClientSession.post
    real_prefix = REAL_HOSTS[host_key]

    def local_post(self: aiohttp.ClientSession, url: str, *args: object, **kwargs: object):
        if url.startswith(real_prefix):
            url = url.replace(real_prefix, local_base, 1)
        return real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "post", local_post)


async def _run_imgbb_upload(tmp_path) -> tuple[str, None]:
    image_path = tmp_path / "spectral.png"
    image_path.write_bytes(b"fake-image-bytes")
    uploader = imgbb.ImageUploader()
    return await uploader.upload_file(str(image_path))


async def _run_ptscreens_upload(tmp_path) -> tuple[str, None]:
    image_path = tmp_path / "spectral.png"
    image_path.write_bytes(b"fake-image-bytes")
    uploader = ptscreens.ImageUploader()
    return await uploader.upload_file(str(image_path))


def test_imgbb_400_surfaces_the_host_error_body(tmp_path, monkeypatch) -> None:
    async def handle_upload(request: web.Request) -> web.Response:
        return web.json_response({"error": {"message": "album not found"}}, status=400)

    async def run() -> None:
        runner = await _serve(handle_upload)
        _redirect_to_local(monkeypatch, "imgbb", _local_url(runner))
        try:
            with pytest.raises(ImageUploadFailed, match="400.*album not found"):
                await _run_imgbb_upload(tmp_path)
        finally:
            await runner.cleanup()

    anyio.run(run)


def test_imgbb_success_still_returns_the_url(tmp_path, monkeypatch) -> None:
    async def handle_upload(request: web.Request) -> web.Response:
        return web.json_response({"data": {"url": "https://i.ibb.co/abc/spectral.png"}})

    async def run() -> None:
        runner = await _serve(handle_upload)
        _redirect_to_local(monkeypatch, "imgbb", _local_url(runner))
        try:
            url, deletion_url = await _run_imgbb_upload(tmp_path)
            assert url == "https://i.ibb.co/abc/spectral.png"
            assert deletion_url is None
        finally:
            await runner.cleanup()

    anyio.run(run)


def test_ptscreens_400_surfaces_the_host_error_body(tmp_path, monkeypatch) -> None:
    async def handle_upload(request: web.Request) -> web.Response:
        return web.json_response({"error": "invalid api key"}, status=400)

    async def run() -> None:
        runner = await _serve(handle_upload)
        _redirect_to_local(monkeypatch, "ptscreens", _local_url(runner))
        try:
            with pytest.raises(ImageUploadFailed, match="400.*invalid api key"):
                await _run_ptscreens_upload(tmp_path)
        finally:
            await runner.cleanup()

    anyio.run(run)


def test_ptscreens_success_still_returns_the_url(tmp_path, monkeypatch) -> None:
    async def handle_upload(request: web.Request) -> web.Response:
        return web.json_response({"image": {"url": "https://ptscreens.com/i/abc.png"}})

    async def run() -> None:
        runner = await _serve(handle_upload)
        _redirect_to_local(monkeypatch, "ptscreens", _local_url(runner))
        try:
            url, deletion_url = await _run_ptscreens_upload(tmp_path)
            assert url == "https://ptscreens.com/i/abc.png"
            assert deletion_url is None
        finally:
            await runner.cleanup()

    anyio.run(run)
