"""salmon resolves names through the OS and still decodes the encodings aiohttp's speedups extra gave it (#682).

aiohttp[speedups] installs aiodns, which makes aiohttp's DefaultResolver its AsyncResolver: c-ares, reading
/etc/resolv.conf itself and skipping NSS, so a machine whose lookups only work through systemd-resolved fails with
"Could not contact DNS servers" (#682, and #279 on Windows). salmon depends on the two codecs it wanted from
the extra instead, so every session it or a library opens resolves names with getaddrinfo, as every other program
on the machine does.
"""

import importlib.util

import aiohttp.resolver
import anyio
import brotli
import pytest
from aiohttp import web

from salmon.sources import base

try:
    from compression import zstd  # pyright: ignore[reportMissingImports]
except ImportError:  # Python < 3.14
    from backports import zstd

PAYLOAD = b'{"title": "Album"}'


def test_names_resolve_through_the_os() -> None:
    assert aiohttp.resolver.DefaultResolver is aiohttp.resolver.ThreadedResolver
    assert importlib.util.find_spec("aiodns") is None


async def _get_json_encoded(encoding: str, body: bytes) -> dict:
    async def handle(request: web.Request) -> web.Response:
        return web.Response(body=body, content_type="application/json", headers={"Content-Encoding": encoding})

    app = web.Application()
    app.router.add_get("/release/1", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        scraper = base.BaseScraper()
        scraper.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        return await scraper.get_json("/release/1")
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    ("encoding", "body"),
    [("br", brotli.compress(PAYLOAD)), ("zstd", zstd.compress(PAYLOAD))],
)
def test_a_compressed_answer_is_decoded(encoding: str, body: bytes) -> None:
    assert anyio.run(_get_json_encoded, encoding, body) == {"title": "Album"}
