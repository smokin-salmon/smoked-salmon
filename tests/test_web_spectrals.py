"""Regression test for #426: the in-process spectrals web server.

#339 removed the separate ``salmon web`` process and moved the server into
``_open_specs_in_web_server`` (src/salmon/uploader/spectrals.py), started while
reviewing spectrals during ``up``. That change shipped with no test. This drives
the real function end to end: it must serve the review page and the spectral
image files over HTTP, and it must clean up its symlink and its server when done.
"""

import tempfile
from pathlib import Path

import aiohttp
import anyio
from aiohttp import web

import salmon.web as web_module
from salmon.uploader import spectrals as uploader_spectrals
from salmon.web import spectrals as web_spectrals

_FULL_BYTES = b"fake full spectral bytes"
_ZOOM_BYTES = b"fake zoom spectral bytes"
_SYMLINK_PATH = Path(web_module.__file__).parent / "static" / "specs"


def _make_specs_dir(tmp_path: Path) -> Path:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "01 Full.png").write_bytes(_FULL_BYTES)
    (specs_dir / "01 Zoom.png").write_bytes(_ZOOM_BYTES)
    return specs_dir


async def _drive_server(specs_path: Path, ids: dict[int, str], requests_fn) -> int:
    """Run ``_open_specs_in_web_server`` on an ephemeral port, let ``requests_fn``
    poke at it in place of the "press enter" prompt, and return the port it used.
    """
    original_create_app_async = uploader_spectrals.create_app_async
    original_prompt_async = uploader_spectrals.prompt_async
    original_port = web_module.web_cfg.port
    original_host = web_module.web_cfg.host
    captured: dict[str, web.AppRunner | int] = {}

    async def capturing_create_app_async() -> web.AppRunner:
        runner = await original_create_app_async()
        captured["runner"] = runner
        return runner

    async def fake_prompt_async(*args, **kwargs) -> None:
        runner = captured["runner"]
        assert isinstance(runner, web.AppRunner)
        port = runner.addresses[0][1]
        captured["port"] = port
        await requests_fn(port)

    web_module.web_cfg.port = 0
    web_module.web_cfg.host = "127.0.0.1"
    uploader_spectrals.create_app_async = capturing_create_app_async
    uploader_spectrals.prompt_async = fake_prompt_async
    try:
        await uploader_spectrals._open_specs_in_web_server(str(specs_path), ids)
    finally:
        uploader_spectrals.create_app_async = original_create_app_async
        uploader_spectrals.prompt_async = original_prompt_async
        web_module.web_cfg.port = original_port
        web_module.web_cfg.host = original_host
        web_spectrals.set_active_spectrals({})

    port = captured["port"]
    assert isinstance(port, int)
    return port


async def _serves_the_spectrals_page_and_static_images() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        specs_path = _make_specs_dir(Path(tmp))

        async def requests_fn(port: int) -> None:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/spectrals") as resp:
                    assert resp.status == 200
                    body = await resp.text()
                    assert "01 Track.flac" in body
                    assert "specs/01 Full.png" in body
                async with session.get(f"http://127.0.0.1:{port}/static/specs/01%20Full.png") as resp:
                    assert resp.status == 200
                    assert await resp.read() == _FULL_BYTES

        port = await _drive_server(specs_path, {1: "01 Track.flac"}, requests_fn)

        assert not _SYMLINK_PATH.exists()
        assert not _SYMLINK_PATH.is_symlink()

        # Nothing should still be listening: a fresh connection must fail.
        connect_failed = False
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(f"http://127.0.0.1:{port}/spectrals", timeout=aiohttp.ClientTimeout(total=1)),
            ):
                pass
        except aiohttp.ClientConnectorError:
            connect_failed = True
        assert connect_failed


async def _answers_404_with_no_active_spectrals() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        specs_path = _make_specs_dir(Path(tmp))

        async def requests_fn(port: int) -> None:
            async with aiohttp.ClientSession() as session, session.get(f"http://127.0.0.1:{port}/spectrals") as resp:
                assert resp.status == 404

        await _drive_server(specs_path, {}, requests_fn)

        assert not _SYMLINK_PATH.exists()
        assert not _SYMLINK_PATH.is_symlink()


def test_serves_the_spectrals_page_and_static_images() -> None:
    try:
        anyio.run(_serves_the_spectrals_page_and_static_images)
    finally:
        web_spectrals.set_active_spectrals({})


def test_answers_404_with_no_active_spectrals() -> None:
    try:
        anyio.run(_answers_404_with_no_active_spectrals)
    finally:
        web_spectrals.set_active_spectrals({})
