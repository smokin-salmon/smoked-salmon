import sys
from pathlib import Path

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.errors import LoginError, RequestFailedError
from salmon.trackers.base import BaseGazelleApi


class CountingLimiter(AsyncLimiter):
    """A limiter loose enough not to slow the tests, which counts the slots taken."""

    def __init__(self) -> None:
        super().__init__(100, 1)
        self.slots = 0

    async def acquire(self, amount: float = 1) -> None:
        self.slots += 1
        await super().acquire(amount)


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = CountingLimiter()
        self._authenticated = True


async def _serve(**handlers) -> tuple[web.AppRunner, str]:
    app = web.Application()
    for path, handler in handlers.items():
        app.router.add_route("*", f"/{path}.php", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


async def _login_redirect_stops_before_the_login_page() -> None:
    hits = []

    async def log(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPFound("/login.php")

    async def login(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPFound("/log.php")

    runner, url = await _serve(log=log, login=login)
    api = FakeApi(url)
    try:
        with pytest.raises(LoginError):
            await api._request("GET", url + "/log.php", params={"page": 1})
        # The bounce to login.php is the answer: it is not requested, nor retried.
        assert hits == ["/log.php"]
    finally:
        await api.close()
        await runner.cleanup()


async def _redirect_hops_take_a_rate_limiter_slot() -> None:
    hits = []

    async def torrents(request: web.Request) -> web.Response:
        hits.append(request.path_qs)
        if "id" not in request.query:
            raise web.HTTPFound(f"torrents.php?id=2&torrentid={request.query['torrentid']}#torrent1")
        return web.Response(text="group page")

    runner, url = await _serve(torrents=torrents)
    api = FakeApi(url)
    try:
        assert await api.get_redirect_torrentgroupid(1) == 2
        assert hits == ["/torrents.php?torrentid=1", "/torrents.php?id=2&torrentid=1"]
        assert api._rate_limiter.slots == len(hits)
    finally:
        await api.close()
        await runner.cleanup()


async def _post_redirect_is_fetched_with_get() -> None:
    hits = []

    async def upload(request: web.Request) -> web.Response:
        hits.append((request.method, request.path))
        assert (await request.post())["auth"] == "an-authkey"
        raise web.HTTPFound("/torrents.php?id=5")

    async def torrents(request: web.Request) -> web.Response:
        hits.append((request.method, request.path))
        return web.Response(text="group page")

    runner, url = await _serve(upload=upload, torrents=torrents)
    api = FakeApi(url)
    try:
        resp = await api._request("POST", url + "/upload.php", data={"auth": "an-authkey"})
        assert hits == [("POST", "/upload.php"), ("GET", "/torrents.php")]
        assert resp.url == url + "/torrents.php?id=5"
        assert resp.text == "group page"
    finally:
        await api.close()
        await runner.cleanup()


async def _redirect_to_another_site_is_not_followed() -> None:
    other_hits = []

    async def elsewhere(request: web.Request) -> web.Response:
        other_hits.append(request.headers.get("Cookie"))
        return web.Response(text="not the tracker")

    other_runner, other_url = await _serve(torrents=elsewhere)

    async def torrents(request: web.Request) -> web.Response:
        raise web.HTTPFound(other_url + "/torrents.php")

    runner, url = await _serve(torrents=torrents)
    api = FakeApi(url)
    try:
        with pytest.raises(RequestFailedError):
            await api._request("GET", url + "/torrents.php")
        # The session cookie must not be carried to another origin.
        assert other_hits == []
    finally:
        await api.close()
        await runner.cleanup()
        await other_runner.cleanup()


async def _redirect_loop_is_cut_short() -> None:
    hits = []

    async def ping(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPFound("/pong.php")

    async def pong(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPFound("/ping.php")

    runner, url = await _serve(ping=ping, pong=pong)
    api = FakeApi(url)
    try:
        with pytest.raises(RequestFailedError):
            await api._request("GET", url + "/ping.php")
        assert hits == ["/ping.php", "/pong.php", "/ping.php"]
    finally:
        await api.close()
        await runner.cleanup()


def test_login_redirect_stops_before_the_login_page() -> None:
    anyio.run(_login_redirect_stops_before_the_login_page)


def test_redirect_hops_take_a_rate_limiter_slot() -> None:
    anyio.run(_redirect_hops_take_a_rate_limiter_slot)


def test_post_redirect_is_fetched_with_get() -> None:
    anyio.run(_post_redirect_is_fetched_with_get)


def test_redirect_to_another_site_is_not_followed() -> None:
    anyio.run(_redirect_to_another_site_is_not_followed)


def test_redirect_loop_is_cut_short() -> None:
    anyio.run(_redirect_loop_is_cut_short)
