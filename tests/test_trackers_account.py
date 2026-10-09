"""Every client of one tracker account shares one rate limit, one lock for requests that are not
idempotent and one pool of kept-alive connections, on each event loop (ADR 0004, #628).

Each test runs a local fake tracker that notes when each request arrives, how many requests that are
not idempotent it is answering at once, and how many connections are open. Most tests shorten the
rate limit's period to one second, which changes the times only, not the rule.
"""

import asyncio
import gc
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from tenacity import wait_none

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.trackers import account
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


def _run(body: Callable[[FakeTracker], Awaitable[None]]) -> FakeTracker:
    """Run a test against a fresh fake tracker on a loop of its own, failing on any session left open."""
    unclosed: list[str] = []
    tracker = FakeTracker()

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        try:
            await body(tracker)
        finally:
            await tracker.runner.cleanup()
        # An unclosed session or connector is only reported once it is collected.
        gc.collect()
        await anyio.sleep(0.05)

    anyio.run(main)
    assert unclosed == []
    return tracker


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without the backoff between attempts, so only the rate limit waits are left."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


@pytest.fixture
def short_period(monkeypatch: pytest.MonkeyPatch) -> float:
    """A rate limit period of one second, with a margin as large for it as the real one is for 10 s."""
    monkeypatch.setattr(account, "RATE_LIMIT_PERIOD", 1.0)
    monkeypatch.setattr(account, "RATE_LIMIT_MARGIN", 0.2)
    return 1.0


def test_two_clients_of_one_tracker_share_one_budget() -> None:
    # The real period: what the tracker sees.
    async def body(tracker: FakeTracker) -> None:
        first, second = _clients(await tracker.start(), "RED", "RED")
        try:
            await asyncio.gather(*(_get(client) for client in (first, second) for _ in range(3)))
        finally:
            await first.close()
            await second.close()

    tracker = _run(body)
    times = tracker.times()
    assert len(times) == 6
    assert _most_within(times, account.RATE_LIMIT_PERIOD) == 5
    # The sixth waits until the first is a period and the margin old, not 2 s as a leaky bucket lets it.
    assert times[5] - times[0] >= account.RATE_LIMIT_PERIOD + account.RATE_LIMIT_MARGIN - 0.05


def test_a_request_waiting_for_a_pooled_connection_enters_the_limit_once_it_has_one(short_period: float) -> None:
    # Two held answers keep both pooled connections busy for a period. A request that entered the
    # limit meanwhile would only reach the tracker once one is free, right next to the requests that
    # entered a period after it.
    async def body(tracker: FakeTracker) -> None:
        (api,) = _clients(await tracker.start(), "RED")
        try:
            await asyncio.gather(*(_get(api, "held") for _ in range(2)), *(_get(api) for _ in range(8)))
        finally:
            await api.close()

    tracker = _run(body)
    assert len(tracker.hits) == 10
    assert _most_within(tracker.times(), short_period) <= 5


def test_requests_not_idempotent_go_one_at_a_time_across_clients() -> None:
    async def body(tracker: FakeTracker) -> None:
        clients = _clients(await tracker.start(), "RED", "RED", unlimited=True)
        try:
            await asyncio.gather(*(_post(client) for client in clients for _ in range(2)))
        finally:
            for client in clients:
                await client.close()

    tracker = _run(body)
    assert [method for _, _, method, _ in tracker.hits] == ["POST"] * 4
    assert tracker.peak_posts == 1


def test_clients_of_one_tracker_open_one_pool_and_one_connection_for_a_post() -> None:
    async def body(tracker: FakeTracker) -> None:
        clients = _clients(await tracker.start(), "RED", "RED", "RED", unlimited=True)
        try:
            await asyncio.gather(*(_get(client, "slow") for client in clients for _ in range(3)))
            await asyncio.gather(
                *(_get(client, "slow") for client in clients for _ in range(3)),
                *(_post(client) for client in clients),
            )
        finally:
            for client in clients:
                await client.close()

    tracker = _run(body)
    assert len(tracker.hits) == 9 + 9 + 3
    # The pool's two connections, and one for the request that is not idempotent in flight.
    assert tracker.peak_connections <= account.POOL_CONNECTIONS + 1
    # The GETs of all three clients went through the same two connections; each POST on one of its own.
    assert len(tracker.transports) == account.POOL_CONNECTIONS + 3


def test_a_429_to_one_client_holds_the_other_clients_requests() -> None:
    # Longer than a leaky bucket's spacing (2 s), which would hold the other request too once filled.
    wait = 4

    async def body(tracker: FakeTracker) -> None:
        limited, other = _clients(await tracker.start(), "RED", "RED")
        tracker.limit_once["limited"] = str(wait)
        try:

            async def other_request_once_limited() -> None:
                await tracker.limited.wait()
                # Once the 429 has reached the client.
                await asyncio.sleep(0.2)
                await _get(other, "other")

            await asyncio.gather(_get(limited, "limited"), other_request_once_limited())
        finally:
            await limited.close()
            await other.close()

    tracker = _run(body)
    (answered_429, *later) = tracker.hits
    assert answered_429[3] == "limited"
    assert sorted(action for _, _, _, action in later) == ["limited", "other"]
    # Neither the request that got the 429 nor the other client's is sent before the wait is over.
    assert all(at - answered_429[0] >= wait - 0.05 for at, _, _, _ in later)


def test_a_429_holds_a_request_already_waiting_for_its_turn() -> None:
    async def body() -> tuple[float, float]:
        limiter = account.SlidingWindowLimiter(1, 0.5)
        async with limiter:
            pass
        started = time.monotonic()

        async def pause_meanwhile() -> None:
            await asyncio.sleep(0.1)
            limiter.pause(1)

        async def enter() -> float:
            async with limiter:
                return time.monotonic()

        entered, _ = await asyncio.gather(enter(), pause_meanwhile())
        return started, entered

    started, entered = anyio.run(body)
    # Its turn came after 0.5 s, but the pause from 0.1 s holds it until 1.1 s.
    assert entered - started >= 1.1 - 0.05


def test_each_tracker_has_its_own_budget(short_period: float) -> None:
    async def body(tracker: FakeTracker) -> None:
        red, ops = _clients(await tracker.start(), "RED", "OPS")
        try:
            # RED's burst first, as when a multi-tracker run checks RED, then OPS.
            await asyncio.gather(*(_get(red) for _ in range(6)), _get(ops))
        finally:
            await red.close()
            await ops.close()

    tracker = _run(body)
    red, (ops,) = tracker.times("RED"), tracker.times("OPS")
    # RED's sixth request waits for its window; OPS's goes out at once.
    assert red[5] - red[0] >= short_period
    assert ops - red[0] < short_period / 2


def test_each_event_loop_has_its_own_state() -> None:
    # One client, used on one loop and then on another, as two anyio.run in one process do.
    api: FakeApi | None = None
    start = 0.0

    async def fill_the_budget(tracker: FakeTracker) -> None:
        nonlocal api
        (api,) = _clients(await tracker.start(), "RED")
        try:
            await asyncio.gather(*(_get(api) for _ in range(account.RATE_LIMIT_REQUESTS)))
        finally:
            await api.close()

    async def one_more(tracker: FakeTracker) -> None:
        nonlocal start
        assert api is not None
        api.base_url = await tracker.start()
        start = time.monotonic()
        try:
            await _get(api)
        finally:
            await api.close()

    _run(fill_the_budget)
    tracker = _run(one_more)
    # The first loop's budget is not this loop's: the request goes out at once.
    assert tracker.times()[0] - start < 1


@pytest.mark.usefixtures("short_period")
def test_loops_in_two_threads_do_not_share_state() -> None:
    errors: list[BaseException] = []
    trackers: list[FakeTracker] = []

    async def body(tracker: FakeTracker) -> None:
        trackers.append(tracker)
        (api,) = _clients(await tracker.start(), "RED")
        try:
            await asyncio.gather(*(_get(api) for _ in range(7)), _post(api))
        finally:
            await api.close()

    def thread() -> None:
        try:
            _run(body)
        except BaseException as err:
            errors.append(err)

    threads = [threading.Thread(target=thread) for _ in range(2)]
    for each in threads:
        each.start()
    for each in threads:
        each.join(timeout=30)
    assert errors == []
    assert [len(tracker.hits) for tracker in trackers] == [8, 8]


def test_closing_one_client_leaves_the_pool_to_the_others() -> None:
    async def body(tracker: FakeTracker) -> None:
        first, second = _clients(await tracker.start(), "RED", "RED", unlimited=True)
        await asyncio.gather(_get(first), _get(second))
        pool = first._session
        assert pool is not None
        assert second._session is pool
        await first.close()
        assert not pool.closed
        # The other client goes on on the same connections.
        await _get(second)
        await second.close()
        assert pool.closed
        # A client closed earlier opens a pool again when used again.
        await _get(first)
        assert first._session is not None
        assert first._session is not pool
        await first.close()

    tracker = _run(body)
    assert len(tracker.hits) == 4


def test_a_command_closes_the_pool_its_clients_share() -> None:
    pools = []

    @click.command()
    async def command() -> None:
        tracker = FakeTracker()
        first, second = _clients(await tracker.start(), "RED", "RED", unlimited=True)
        try:
            await asyncio.gather(_get(first), _get(second))
            pools.append(first._session)
        finally:
            await tracker.runner.cleanup()

    command(args=[], standalone_mode=False)
    assert len(pools) == 1
    assert pools[0] is not None
    assert pools[0].closed
