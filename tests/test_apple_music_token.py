import sys
from pathlib import Path

import anyio
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.sources.apple_music import AppleMusicBase

HOMEPAGE_HTML = '<script src="/assets/index~abc123.js"></script>'


def _bundle_with_token(token: str) -> str:
    return f'var config={{token:"{token}"}};'


async def _serve(js_body: str) -> web.AppRunner:
    async def handle_home(request: web.Request) -> web.Response:
        return web.Response(text=HOMEPAGE_HTML, content_type="text/html")

    async def handle_js(request: web.Request) -> web.Response:
        return web.Response(text=js_body, content_type="application/javascript")

    app = web.Application()
    app.router.add_get("/", handle_home)
    app.router.add_get("/assets/index~abc123.js", handle_js)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


def _url(runner: web.AppRunner) -> str:
    return f"http://127.0.0.1:{runner.addresses[0][1]}"


async def _get_token_finds_typ_first_header() -> None:
    token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9." + "a" * 40 + "." + "b" * 40
    runner = await _serve(_bundle_with_token(token))
    AppleMusicBase._token = None
    import salmon.sources.apple_music as apple_music_module

    original_url = apple_music_module.APPLE_MUSIC_URL
    apple_music_module.APPLE_MUSIC_URL = _url(runner)
    try:
        found = await AppleMusicBase._get_token()
        assert found == token
    finally:
        apple_music_module.APPLE_MUSIC_URL = original_url
        AppleMusicBase._token = None
        await runner.cleanup()


async def _get_token_finds_alg_first_header() -> None:
    token = "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9." + "c" * 40 + "." + "d" * 40
    runner = await _serve(_bundle_with_token(token))
    AppleMusicBase._token = None
    import salmon.sources.apple_music as apple_music_module

    original_url = apple_music_module.APPLE_MUSIC_URL
    apple_music_module.APPLE_MUSIC_URL = _url(runner)
    try:
        found = await AppleMusicBase._get_token()
        assert found == token
    finally:
        apple_music_module.APPLE_MUSIC_URL = original_url
        AppleMusicBase._token = None
        await runner.cleanup()


def test_get_token_finds_typ_first_header() -> None:
    anyio.run(_get_token_finds_typ_first_header)


def test_get_token_finds_alg_first_header() -> None:
    anyio.run(_get_token_finds_alg_first_header)
