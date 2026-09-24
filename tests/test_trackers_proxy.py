"""Tracker requests through a proxy (#406): the same requests, connections and retries as without one.

Every tracker request goes through BaseGazelleApi._request, its rate limiter, its small shared pool,
and the one-off session a state-changing request gets. A proxy must change none of that, only the
route. A proxy failure happens before the request is written, so it is retried as a request the
tracker never got; a request the proxy did pass on is not. Tests talk only to a local fake tracker,
through a local fake proxy. With a proxy, the tracker is named under .invalid, a name only the proxy
resolves: a client that looked it up locally could not connect.
"""

import asyncio
import gc
import traceback
from collections.abc import Awaitable, Callable

import aiohttp
import anyio
import pytest
from aiohttp import web
from aiohttp_socks import ProxyConnector
from aiolimiter import AsyncLimiter
from fake_proxy import FakeProxy
from tenacity import stop_after_attempt, wait_none

from salmon import cfg
from salmon.config.validations import ProxyCfg, ProxyServicesCfg
from salmon.errors import UnknownOutcomeError
from salmon.trackers.base import BaseGazelleApi, HttpResponse, RetryableError

PASSWORD = "s3cr3t-proxy-pass"
AUTH = ("salmon", PASSWORD)

# Each kind of proxy, with credentials where the protocol has them.
PROXIES = [("socks5", AUTH), ("http", AUTH), ("socks4", None)]


class CountingLimiter(AsyncLimiter):
    """Counts the requests it lets through, and can run a step before a given one goes out."""

    def __init__(self) -> None:
        super().__init__(100, 1)
        self.acquired = 0
        self.before: dict[int, Callable[[], object]] = {}

    async def acquire(self, amount: float = 1) -> None:
        await super().acquire(amount)
        self.acquired += 1
        if step := self.before.get(self.acquired):
            step()


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


class FakeTracker:
    """A local tracker logging each request it gets, its headers, and the port it came from."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.ports: set[int] = set()
        # (Authorization, Cookie) of each request.
        self.sent: list[tuple[str | None, str | None]] = []

    async def start(self) -> int:
        app = web.Application()
        app.router.add_route("*", "/{page}.php", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return self.runner.addresses[0][1]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        what = request.query.get("action", request.match_info["page"])
        assert request.transport is not None
        self.requests.append(f"{request.method} {what}")
        self.ports.add(request.transport.get_extra_info("peername")[1])
        self.sent.append((request.headers.get("Authorization"), request.headers.get("Cookie")))
        if what == "drop":
            # The tracker has the request, but the connection drops before its answer.
            request.transport.close()
        elif request.match_info["page"] == "upload":
            raise web.HTTPFound("/torrents.php?id=5")
        return web.json_response({"status": "success", "response": {}})


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting, so the tests count attempts, not seconds."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


def _use_proxy(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    """Give RED the proxy `url`, and every other service a proxy that must not be used."""
    others = dict.fromkeys(ProxyServicesCfg.__struct_fields__, "socks5://127.0.0.1:9")
    monkeypatch.setattr(cfg, "proxy", ProxyCfg(services=ProxyServicesCfg(**{**others, "red": url or ""})))


Body = Callable[[FakeTracker, FakeApi], Awaitable[None]]


def _run(body: Body, *, proxied: bool) -> None:
    """Run a test against a fresh fake tracker and client, then fail on anything left unclosed."""
    unclosed: list[str] = []

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        tracker = FakeTracker()
        port = await tracker.start()
        api = FakeApi(f"http://{'tracker.invalid' if proxied else '127.0.0.1'}:{port}")
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


def _get(api: FakeApi, action: str) -> Awaitable[HttpResponse]:
    return api._request("GET", api.base_url + "/ajax.php", params={"action": action})


def _post(api: FakeApi, action: str) -> Awaitable[HttpResponse]:
    return api._request("POST", api.base_url + "/ajax.php", params={"action": action}, data={"file": "x"})


async def _upload_session(tracker: FakeTracker, api: FakeApi) -> tuple[list[str], list, int, int]:
    """Send what an upload sends; return the requests, their headers, connections and limiter count."""
    # Gathered calls, as the dupe checks send them, through the shared pool of two connections.
    await asyncio.gather(*(_get(api, "index") for _ in range(4)))
    # An API key upload, on a connection of its own.
    api.api_key = "an-api-key"
    await api._request("POST", api.base_url + "/ajax.php?action=upload", data={"file": "x"}, prefer_api_key=True)
    # A site page upload, on a connection of its own, and the redirect after it on the same one.
    await api._request("POST", api.base_url + "/upload.php", data={"file": "x"})
    # Back on the pool.
    await _get(api, "index")
    return tracker.requests, tracker.sent, len(tracker.ports), api.limiter.acquired


@pytest.mark.parametrize(("kind", "auth"), PROXIES)
def test_through_a_proxy_the_tracker_gets_the_same_requests_on_as_many_connections(
    monkeypatch: pytest.MonkeyPatch, kind: str, auth: tuple[str, str] | None
) -> None:
    measured: dict[str, tuple] = {}
    proxy = FakeProxy(kind, auth=auth)

    async def direct(tracker: FakeTracker, api: FakeApi) -> None:
        measured["direct"] = await _upload_session(tracker, api)

    async def proxied(tracker: FakeTracker, api: FakeApi) -> None:
        _use_proxy(monkeypatch, await proxy.start())
        try:
            measured["proxied"] = await _upload_session(tracker, api)
        finally:
            await proxy.stop()

    _use_proxy(monkeypatch, None)
    _run(direct, proxied=False)
    _run(proxied, proxied=True)

    requests, sent, connections, limited = measured["proxied"]
    assert measured["proxied"] == measured["direct"]
    assert requests == ["GET index"] * 4 + ["POST upload", "POST upload", "GET torrents", "GET index"]
    # The API key request without the cookie, the others with the cookie and no API key.
    cookie = (None, "session=a-cookie")
    assert sent == [cookie] * 4 + [("an-api-key", None)] + [cookie] * 3
    # Two pooled connections, and one of its own for each POST.
    assert connections == 4
    assert limited == 8
    # Each of them through the proxy, which got the tracker's name to resolve.
    assert proxy.connections == 4
    assert len(proxy.targets) == 4
    assert {host for host, _ in proxy.targets} == {"tracker.invalid"}
    if kind != "http":
        assert all(proxy.by_name)


@pytest.mark.parametrize("proxied", [False, True])
def test_the_pool_and_a_posts_own_session_keep_their_limits_and_cookie_jar(
    monkeypatch: pytest.MonkeyPatch, proxied: bool
) -> None:
    _use_proxy(monkeypatch, "socks5://127.0.0.1:1080" if proxied else None)

    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        async with api._session_for(idempotent=False) as own:
            sessions = {"pool": (api._http_session(), 2), "own": (own, 1)}
            for session, limit in sessions.values():
                assert session.connector is not None
                assert session.connector.limit == limit
                assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
                if proxied:
                    assert isinstance(session.connector, ProxyConnector)
                else:
                    assert type(session.connector) is aiohttp.TCPConnector

    _run(body, proxied=proxied)


def test_without_a_proxy_the_proxy_sees_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = FakeProxy("socks5")

    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        # A proxy is configured for every other service, but not for this tracker.
        others = dict.fromkeys(ProxyServicesCfg.__struct_fields__, await proxy.start())
        monkeypatch.setattr(cfg, "proxy", ProxyCfg(services=ProxyServicesCfg(**{**others, "red": ""})))
        try:
            await _upload_session(tracker, api)
        finally:
            await proxy.stop()
        assert len(tracker.requests) == 8
        assert proxy.connections == 0

    _run(body, proxied=False)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "failure", ["proxy down", "proxy refuses", "wrong password", "proxy stalls", "tls fails through the proxy"]
)
def test_a_request_the_proxy_could_not_pass_on_is_retried_as_never_sent(
    monkeypatch: pytest.MonkeyPatch, method: str, failure: str
) -> None:
    kind = "http" if failure == "wrong password" else "socks5"
    proxy = FakeProxy(kind, auth=AUTH, refuse=failure == "proxy refuses", stall=failure == "proxy stalls")
    # A stalled proxy is only given up on at the connect timeout: fewer, shorter attempts.
    attempts, timeout = (2, 1) if failure == "proxy stalls" else (5, 10)
    monkeypatch.setattr(BaseGazelleApi._send.retry, "stop", stop_after_attempt(attempts))  # type: ignore[attr-defined]

    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        url = await proxy.start()
        if failure == "proxy down":
            await proxy.stop()
        if failure == "wrong password":
            url = proxy.url(auth=("salmon", "not-the-password"))
        _use_proxy(monkeypatch, url)
        if failure == "tls fails through the proxy":
            # The tracker speaks plain HTTP: the TLS handshake with it fails, through the tunnel.
            api.base_url = api.base_url.replace("http:", "https:")
        try:
            with pytest.raises(RetryableError) as raised:
                await api._request(
                    method, api.base_url + "/ajax.php", params={"action": "upload"}, timeout_secs=timeout
                )
        finally:
            if failure != "proxy down":
                await proxy.stop()
        assert BaseGazelleApi._send.statistics["attempt_number"] == attempts  # type: ignore[attr-defined]
        assert api.limiter.acquired == attempts
        assert tracker.requests == []
        # Every attempt went to the proxy (a proxy that is down gets them too, and refuses them).
        assert proxy.connections == (0 if failure == "proxy down" else attempts)
        cause = raised.value.__cause__
        expected = TimeoutError if failure == "proxy stalls" else aiohttp.ClientConnectorError
        assert isinstance(cause, expected)
        if failure in ("proxy down", "proxy refuses", "wrong password"):
            assert "proxy" in str(raised.value)

    _run(body, proxied=True)


def test_a_post_the_proxy_refused_goes_through_once_it_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = FakeProxy("socks5", refuse=True)

    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        _use_proxy(monkeypatch, await proxy.start())

        def accept() -> None:
            proxy.refuse = False

        api.limiter.before[2] = accept
        try:
            resp = await _post(api, "upload")
        finally:
            await proxy.stop()
        assert resp.status == 200
        assert tracker.requests == ["POST upload"]
        assert BaseGazelleApi._send.statistics["attempt_number"] == 2  # type: ignore[attr-defined]
        assert len(proxy.targets) == 2

    _run(body, proxied=True)


@pytest.mark.parametrize(("kind", "auth"), PROXIES)
def test_a_post_the_proxy_passed_on_is_not_sent_again_when_its_answer_is_lost(
    monkeypatch: pytest.MonkeyPatch, kind: str, auth: tuple[str, str] | None
) -> None:
    proxy = FakeProxy(kind, auth=auth)

    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        _use_proxy(monkeypatch, await proxy.start())
        try:
            with pytest.raises(UnknownOutcomeError):
                await _post(api, "drop")
        finally:
            await proxy.stop()
        assert tracker.requests == ["POST drop"]
        assert api.limiter.acquired == 1
        assert len(proxy.targets) == 1

    _run(body, proxied=True)


@pytest.mark.parametrize("failure", ["proxy down", "wrong password", "answer lost", "success"])
def test_the_proxy_password_is_never_shown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", True)
    proxy = FakeProxy("http", auth=("salmon", "the-right-one" if failure == "wrong password" else PASSWORD))
    shown: list[str] = []

    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        await proxy.start()
        _use_proxy(monkeypatch, proxy.url(auth=AUTH))
        if failure == "proxy down":
            await proxy.stop()
        try:
            await _post(api, "drop" if failure == "answer lost" else "upload")
        except Exception as e:
            shown.append("".join(traceback.format_exception(e)))
        finally:
            if failure != "proxy down":
                await proxy.stop()

    _run(body, proxied=True)
    assert shown or failure == "success"
    shown.append("".join(capsys.readouterr()))
    assert "[DEBUG] POST" in shown[-1]
    assert not [text for text in shown if PASSWORD in text]
