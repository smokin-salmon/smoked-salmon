"""Helpers of tests/test_ssrf.py that other test modules use too."""

import contextlib
import io
import ipaddress
from collections.abc import AsyncIterator

from aiohttp import web
from PIL import Image

LOOPBACK = (ipaddress.ip_network("127.0.0.1/32"),)


def _png() -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(data, "PNG")
    return data.getvalue()


def bandcamp_page(cover_url: str = "") -> str:
    """An album page as Bandcamp's scraper reads it."""
    return f"""<html><body>
      <div id="name-section"><div class="trackTitle">The Album</div><h3>by <span>The Artist</span></h3></div>
      <div id="tralbumArt"><img src="{cover_url}"></div>
      <div class="tralbumData tralbum-credits">released March 4, 2022</div>
    </body></html>"""


class FakeStore:
    """A store on 127.0.0.1: album pages at /album/<name>, a cover at /cover.png, a redirect to `moved_to` at /moved.

    Every request it gets is kept, by path.
    """

    def __init__(self, page: str | None = None) -> None:
        self.requests: list[str] = []
        self.page = page
        self.moved_to = ""

    @contextlib.asynccontextmanager
    async def serving(self) -> AsyncIterator["FakeStore"]:
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._answer)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.port = runner.addresses[0][1]
        try:
            yield self
        finally:
            await runner.cleanup()

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{self.port}{path}"

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request.path)
        if request.path == "/moved":
            raise web.HTTPFound(self.moved_to)
        if request.path == "/cover.png":
            return web.Response(body=_png(), content_type="image/png")
        return web.Response(text=self.page or bandcamp_page(), content_type="text/html")
