"""A request that is not idempotent goes out on a connection of its own, and leaves the client's pool alone (#472).

A state-changing request (an upload, a report, an image upload) is sent on a new connection, as one that
fails on a pooled connection the tracker was closing as idle cannot be told from one the tracker acted on
(#446). It used to get one by closing the client's pool, which cut off every other request in flight on
that client. It now goes through a session of its own, closed as soon as it is done. Tests talk only to a
local fake tracker.
"""

import asyncio
import gc
import socket
from collections.abc import Awaitable, Callable

import aiohttp
import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from tenacity import wait_none

from salmon.errors import RequestFailedError, UnknownOutcomeError
from salmon.trackers.base import BaseGazelleApi, HttpResponse

# How long the fake tracker holds a slow answer.
HOLD = 0.3


class CountingLimiter(AsyncLimiter):
    """Counts the requests it lets through, and can run a step before a given one goes out."""

    def __init__(self) -> None:
        super().__init__(100, 1)
        self.acquired = 0
        self.before: dict[int, Callable[[], Awaitable[object]]] = {}

    async def acquire(self, amount: float = 1) -> None:
        await super().acquire(amount)
        self.acquired += 1
        if step := self.before.get(self.acquired):
            await step()


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self.limiter = CountingLimiter()
        self._rate_limiter = self.limiter
        self._authenticated = True
        # Every session this client made for one request of its own.
        self.own_sessions: list[aiohttp.ClientSession] = []

    def _new_session(self, connections: int) -> aiohttp.ClientSession:
        session = super()._new_session(connections)
        if connections == 1:
            self.own_sessions.append(session)
        return session


class FakeTracker:
    """A local tracker logging each request it gets and the client port it came from."""

    def __init__(self) -> None:
        self.hits: list[tuple[str, int]] = []
        # (Authorization, Cookie) of each request.
        self.sent: list[tuple[str | None, str | None]] = []
        self.transports: list[asyncio.BaseTransport] = []
        self.arrived = asyncio.Event()
        # Requests being answered now, and the most at once.
        self.open = 0
        self.peak = 0

    async def start(self, port: int = 0) -> str:
        app = web.Application()
        app.router.add_route("*", "/{page}.php", self.handle)
        # A held answer stops when the client hangs up, as a real server's would.
        self.runner = web.AppRunner(app, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", port).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    @property
    def requests(self) -> list[str]:
        return [what for what, _ in self.hits]

    @property
    def ports(self) -> list[int]:
        return [port for _, port in self.hits]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            return await self.answer(request)
        finally:
            self.open -= 1

    async def answer(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        what = request.query.get("action", request.match_info["page"])
        assert request.transport is not None
        self.hits.append((f"{request.method} {what}", request.transport.get_extra_info("peername")[1]))
        self.transports.append(request.transport)
        self.sent.append((request.headers.get("Authorization"), request.headers.get("Cookie")))
        self.arrived.set()
        if what in ("slow", "hang"):
            await asyncio.sleep(HOLD if what == "slow" else 30)
        elif what == "drop":
            # The tracker has the request, but the connection drops before its answer.
            request.transport.close()
        elif what == "reject":
            return web.json_response({"status": "failure", "error": "bad form"}, status=400)
        elif request.match_info["page"] == "upload":
            raise web.HTTPFound("/torrents.php?id=5", headers={"Set-Cookie": "planted=by-the-server; Path=/"})
        return web.json_response({"status": "success", "response": {}})


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting, so the tests count attempts, not seconds."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


def _run(body: Callable[[FakeTracker, FakeApi], Awaitable[None]], port: int = 0) -> None:
    """Run a test against a fresh fake tracker and client, then fail on anything left unclosed."""
    unclosed: list[str] = []

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        tracker = FakeTracker()
        api = FakeApi(await tracker.start(port))
        try:
            await body(tracker, api)
        finally:
            await api.close()
            await tracker.runner.cleanup()
        del tracker, api
        # An unclosed session or connector is only reported once it is collected.
        gc.collect()
        await anyio.sleep(0.05)

    anyio.run(main)
    assert unclosed == []


def _post(api: FakeApi, action: str) -> Awaitable[HttpResponse]:
    return api._request("POST", api.base_url + "/ajax.php", params={"action": action}, data={"file": "x"})


def _get(api: FakeApi, action: str) -> Awaitable[HttpResponse]:
    return api._request("GET", api.base_url + "/ajax.php", params={"action": action})


def test_a_post_leaves_a_request_in_flight_on_the_same_client_alone() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        async def post_while_the_get_is_held() -> HttpResponse:
            await tracker.arrived.wait()
            return await _post(api, "upload")

        get, post = await asyncio.gather(_get(api, "slow"), post_while_the_get_is_held())
        assert (get.status, post.status) == (200, 200)
        # The held GET was not cut off, so it was not sent again.
        assert tracker.requests == ["GET slow", "POST upload"]
        get_port, post_port = tracker.ports
        assert post_port != get_port
        assert api.limiter.acquired == 2

    _run(body)


def test_gathered_posts_through_one_client_all_go_through_one_at_a_time() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        answers = await asyncio.gather(*(_post(api, "slow") for _ in range(3)))
        assert [answer.status for answer in answers] == [200] * 3
        assert tracker.requests == ["POST slow"] * 3
        # Each on a new connection, never two at once: no burst of handshakes.
        assert len(set(tracker.ports)) == 3
        assert tracker.peak == 1
        assert api.limiter.acquired == 3

    _run(body)


def test_a_get_is_not_held_up_by_a_post_in_flight() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        done: list[str] = []

        async def post() -> None:
            await _post(api, "slow")
            done.append("POST")

        async def get_while_the_post_is_held() -> None:
            await tracker.arrived.wait()
            await _get(api, "index")
            done.append("GET")

        await asyncio.gather(post(), get_while_the_post_is_held())
        assert tracker.requests == ["POST slow", "GET index"]
        assert done == ["GET", "POST"]
        assert tracker.peak == 2

    _run(body)


def test_the_request_after_a_post_reuses_the_pools_kept_alive_connection() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        await _get(api, "index")
        pool = api._session
        await _post(api, "upload")
        await _get(api, "index")
        assert tracker.requests == ["GET index", "POST upload", "GET index"]
        before, post, after = tracker.ports
        assert post != before
        assert after == before
        assert api._session is pool
        assert api.limiter.acquired == 3

    _run(body)


def test_a_post_and_its_redirect_share_one_new_connection() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        await _get(api, "index")
        # A site page upload: the tracker answers with a redirect to the group it went into.
        resp = await api._request("POST", api.base_url + "/upload.php", data={"file": "x"})
        await _get(api, "index")
        assert resp.url.endswith("/torrents.php?id=5")
        assert tracker.requests == ["GET index", "POST upload", "GET torrents", "GET index"]
        before, post, redirect, after = tracker.ports
        assert redirect == post != before
        assert after == before
        assert api.limiter.acquired == 4

    _run(body)


def test_a_posts_own_session_keeps_the_header_rules() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        # A named host, because a cookie jar discards cookies set by a bare IP address.
        api.base_url = api.base_url.replace("127.0.0.1", "localhost")
        api.api_key = "an-api-key"
        await api._request("POST", api.base_url + "/ajax.php?action=upload", data={"file": "x"}, prefer_api_key=True)
        await api._request("POST", api.base_url + "/upload.php", data={"file": "x"})
        assert tracker.requests == ["POST upload", "POST upload", "GET torrents"]
        assert tracker.sent == [
            ("an-api-key", None),
            (None, "session=a-cookie"),
            # The redirect hop does not carry the cookie the tracker set on the way.
            (None, "session=a-cookie"),
        ]

    _run(body)


def test_a_post_whose_answer_is_lost_is_sent_once() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        with pytest.raises(UnknownOutcomeError):
            await _post(api, "drop")
        assert tracker.requests == ["POST drop"]
        assert api.limiter.acquired == 1

    _run(body)


def test_a_post_that_could_not_connect_is_sent_again_and_goes_through() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        # Nothing listens for the first attempt; the tracker is up for the second.
        await tracker.runner.cleanup()
        api.limiter.before[2] = lambda: tracker.start(port)
        resp = await _post(api, "upload")
        assert resp.status == 200
        assert tracker.requests == ["POST upload"]
        assert BaseGazelleApi._send.statistics["attempt_number"] == 2  # type: ignore[attr-defined]
        assert api.limiter.acquired == 2
        # A new connection of its own for each attempt, each closed once it was done.
        assert len(api.own_sessions) == 2
        assert all(session.closed for session in api.own_sessions)

    _run(body, port)


@pytest.mark.parametrize(
    ("action", "raised"),
    [("upload", None), ("reject", RequestFailedError), ("drop", UnknownOutcomeError), ("hang", TimeoutError)],
)
def test_a_posts_own_connection_is_closed_however_the_post_ends(action: str, raised: type[Exception] | None) -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        await _get(api, "index")
        pool = api._session
        if raised is None:
            await _post(api, action)
        else:
            with pytest.raises(raised):
                # A hung POST is cancelled, as when a timeout or Ctrl-C ends the run around it.
                with anyio.fail_after(HOLD):
                    await _post(api, action)
        assert tracker.requests == ["GET index", f"POST {action}"]
        assert len(api.own_sessions) == 1
        assert api.own_sessions[0].closed
        await anyio.sleep(0.05)
        assert tracker.transports[1].is_closing()
        # The pool is left as it was, its connection kept alive.
        assert api._session is pool
        assert pool is not None and not pool.closed
        assert not tracker.transports[0].is_closing()
        # The next POST does not wait for the one that ended.
        with anyio.fail_after(1):
            await _post(api, "upload")

    _run(body)
