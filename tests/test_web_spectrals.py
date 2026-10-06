"""Regression test for #426: the in-process spectrals web server.

#339 removed the separate ``salmon web`` process and moved the server into
``_open_specs_in_web_server`` (src/salmon/uploader/spectrals.py), started while
reviewing spectrals during ``up``. That change shipped with no test. This drives
the real function end to end: it must serve the review page and the spectral
image files over HTTP, and it must stop its server when done. It serves the spectrals
folder in place: nothing is linked or written into the installed package (a directory
symlink there failed on Windows with WinError 1314).
"""

import errno
import os
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
_STATIC_DIR = Path(web_module.__file__).parent / "static"


def _static_tree() -> set[str]:
    return {str(p.relative_to(_STATIC_DIR)) for p in _STATIC_DIR.rglob("*")}


def _make_specs_dir(tmp_path: Path, name: str = "specs", full: bytes = _FULL_BYTES, zoom: bytes = _ZOOM_BYTES) -> Path:
    specs_dir = tmp_path / name
    specs_dir.mkdir()
    (specs_dir / "01 Full.png").write_bytes(full)
    (specs_dir / "01 Zoom.png").write_bytes(zoom)
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

    async def capturing_create_app_async(*args: str) -> web.AppRunner:
        runner = await original_create_app_async(*args)
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
        static_before = _static_tree()

        async def requests_fn(port: int) -> None:
            # Nothing is written into the installed package, not even while the server runs.
            assert _static_tree() == static_before
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
        assert _static_tree() == static_before

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


async def _shows_each_tracks_frequency_plot_under_its_spectrals() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        specs_path = _make_specs_dir(Path(tmp))
        (specs_path / "01 Spectrum.png").write_bytes(b"fake frequency plot bytes")
        (specs_path / "02 Full.png").write_bytes(_FULL_BYTES)
        (specs_path / "02 Zoom.png").write_bytes(_ZOOM_BYTES)

        async def requests_fn(port: int) -> None:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/spectrals") as resp:
                    body = await resp.text()
                    assert "specs/01 Spectrum.png" in body
                    # Track 2 was not measured (a failed analysis writes no plot): no broken image.
                    assert "specs/02 Full.png" in body
                    assert "02 Spectrum.png" not in body
                async with session.get(f"http://127.0.0.1:{port}/static/specs/01%20Spectrum.png") as resp:
                    assert resp.status == 200

        await _drive_server(specs_path, {1: "01 Track.flac", 2: "02 Track.flac"}, requests_fn)


def test_shows_each_tracks_frequency_plot_under_its_spectrals() -> None:
    try:
        anyio.run(_shows_each_tracks_frequency_plot_under_its_spectrals)
    finally:
        web_spectrals.set_active_spectrals({})


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


async def _serves_the_images_where_symlinks_are_not_allowed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        specs_path = _make_specs_dir(Path(tmp))
        static_before = _static_tree()

        async def requests_fn(port: int) -> None:
            assert _static_tree() == static_before
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/spectrals") as resp:
                    assert resp.status == 200
                async with session.get(f"http://127.0.0.1:{port}/static/specs/01%20Full.png") as resp:
                    assert resp.status == 200
                    assert await resp.read() == _FULL_BYTES
                async with session.get(f"http://127.0.0.1:{port}/static/specs/01%20Zoom.png") as resp:
                    assert resp.status == 200
                    assert await resp.read() == _ZOOM_BYTES

        await _drive_server(specs_path, {1: "01 Track.flac"}, requests_fn)
        assert _static_tree() == static_before
        assert sorted(os.listdir(specs_path)) == ["01 Full.png", "01 Zoom.png"]


def test_serves_the_images_where_symlinks_are_not_allowed(monkeypatch) -> None:
    def no_symlink(*args, **kwargs):
        # What Windows raises for a directory symlink without admin rights or Developer Mode.
        raise OSError(errno.EPERM, "A required privilege is not held by the client")

    monkeypatch.setattr(os, "symlink", no_symlink)
    try:
        anyio.run(_serves_the_images_where_symlinks_are_not_allowed)
    finally:
        web_spectrals.set_active_spectrals({})


async def _serves_each_folder_its_own_images() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        first = _make_specs_dir(Path(tmp), "first", b"first full", b"first zoom")
        second = _make_specs_dir(Path(tmp), "second", b"second full", b"second zoom")

        for specs_path, full, zoom in ((first, b"first full", b"first zoom"), (second, b"second full", b"second zoom")):

            async def requests_fn(port: int, full: bytes = full, zoom: bytes = zoom) -> None:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"http://127.0.0.1:{port}/static/specs/01%20Full.png") as resp:
                        assert await resp.read() == full
                    async with session.get(f"http://127.0.0.1:{port}/static/specs/01%20Zoom.png") as resp:
                        assert await resp.read() == zoom

            await _drive_server(specs_path, {1: "01 Track.flac"}, requests_fn)


def test_serves_each_folder_its_own_images() -> None:
    try:
        anyio.run(_serves_each_folder_its_own_images)
    finally:
        web_spectrals.set_active_spectrals({})
