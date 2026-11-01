"""Helpers of tests/test_trackers_account.py that other test modules use too."""

import asyncio
import time
from collections.abc import Awaitable

from aiohttp import web
from aiolimiter import AsyncLimiter

from salmon.trackers.base import BaseGazelleApi, HttpResponse

# How long the fake tracker takes to answer a "slow" request, a "held" one and a POST.
SLOW = 0.6


HELD = 1.0


POST_TIME = 0.3


class FakeApi(BaseGazelleApi):
    cookie = "a-cookie"

    def __init__(self, base_url: str, site_code: str = "RED") -> None:
        self.base_url = base_url
        self.site_code = site_code
        self.site_string = site_code
        super().__init__()
        self._authenticated = True


class FakeTracker:
    """A local tracker noting each request it gets, when, and the connections open at the time."""

    def __init__(self) -> None:
        # (arrival time, site code the client sent, method, action) of each request.
        self.hits: list[tuple[float, str, str, str]] = []
        self.transports: list[asyncio.BaseTransport] = []
        # Connections open when a request arrives, at most; requests that are not idempotent answered at once.
        self.peak_connections = 0
        self.posts = 0
        self.peak_posts = 0
        # Answered 429 once each, with this Retry-After.
        self.limit_once: dict[str, str] = {}
        self.limited = asyncio.Event()

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("*", "/ajax.php", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    def times(self, site: str | None = None) -> list[float]:
        return [at for at, sent_by, _, _ in self.hits if site in (None, sent_by)]

    async def handle(self, request: web.Request) -> web.Response:
        await request.read()
        action = request.query.get("action", "")
        self.hits.append((time.monotonic(), request.headers["X-Site"], request.method, action))
        assert request.transport is not None
        if request.transport not in self.transports:
            self.transports.append(request.transport)
        self.peak_connections = max(
            self.peak_connections, sum(not transport.is_closing() for transport in self.transports)
        )
        if retry_after := self.limit_once.pop(action, None):
            self.limited.set()
            return web.json_response(
                {"status": "failure", "error": "Rate limit exceeded"}, status=429, headers={"Retry-After": retry_after}
            )
        if request.method == "POST":
            self.posts += 1
            self.peak_posts = max(self.peak_posts, self.posts)
            try:
                await asyncio.sleep(POST_TIME)
            finally:
                self.posts -= 1
        elif action in ("slow", "held"):
            await asyncio.sleep(SLOW if action == "slow" else HELD)
        return web.json_response({"status": "success", "response": {}})


def _get(api: FakeApi, action: str = "browse") -> Awaitable[HttpResponse]:
    return api._request("GET", api.base_url + "/ajax.php", params={"action": action})


def _post(api: FakeApi) -> Awaitable[HttpResponse]:
    return api._request("POST", api.base_url + "/ajax.php", params={"action": "upload"}, data={"file": "x"})


def _clients(base_url: str, *site_codes: str, unlimited: bool = False) -> list[FakeApi]:
    """Clients of the fake, one per site code given.

    Args:
        unlimited: Give them one rate limiter no test fills, for the tests about the lock and the pool.
    """
    clients = [FakeApi(base_url, site_code) for site_code in site_codes]
    limiter = AsyncLimiter(1000, 1)
    for client in clients:
        # Lets the fake tell the trackers apart, as they all talk to it.
        client.headers["X-Site"] = client.site_code
        if unlimited:
            client._rate_limiter = limiter
    return clients


def _most_within(times: list[float], period: float) -> int:
    """The most requests that arrived within any `period` seconds."""
    times = sorted(times)
    return max((sum(1 for later in times[i:] if later - at < period) for i, at in enumerate(times)), default=0)
