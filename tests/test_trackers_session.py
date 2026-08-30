import asyncio
import sys
from pathlib import Path

import anyio
from aiohttp import web
from aiolimiter import AsyncLimiter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.trackers.base import BaseGazelleApi


class FakeApi(BaseGazelleApi):
    cookie = "fake-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        super().__init__()
        # Shadow the 5-per-10s limiter so the tests measure connection reuse, not
        # throttling. Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


async def _serve(handler) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/ajax.php", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


def _url(runner: web.AppRunner, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{runner.addresses[0][1]}"


def _ok() -> web.Response:
    return web.json_response({"status": "success", "response": {"authkey": "a", "passkey": "p"}})


async def _gathered_requests_share_one_connection() -> None:
    peers = []

    async def handle_ajax(request: web.Request) -> web.Response:
        peers.append(request.transport.get_extra_info("peername"))
        return _ok()

    runner = await _serve(handle_ajax)
    api = FakeApi(_url(runner))
    try:
        await asyncio.gather(
            *(
                api._request("GET", api.base_url + "/ajax.php", params={"action": "index"}, timeout_secs=10)
                for _ in range(6)
            )
        )
        assert len(peers) == 6
        assert len({peer[1] for peer in peers}) == 1
    finally:
        await api.close()
        await runner.cleanup()


async def _api_key_requests_stay_cookie_free() -> None:
    sent_cookies = []

    async def handle_ajax(request: web.Request) -> web.Response:
        sent_cookies.append(request.headers.get("Cookie"))
        response = _ok()
        response.set_cookie("planted", "by-the-server")
        return response

    runner = await _serve(handle_ajax)
    # A named host, because a cookie jar discards cookies set by a bare IP address.
    api = FakeApi(_url(runner, "localhost"))
    api.api_key = "an-api-key"
    try:
        url = api.base_url + "/ajax.php"
        await api._request("GET", url, params={"action": "index"})
        await api._request("GET", url, params={"action": "index"}, prefer_api_key=True)
        assert "session=fake-cookie" in sent_cookies[0]
        # The shared session must not carry the server's cookie into a request that
        # authenticates by api key, which is meant to send no cookie at all.
        assert sent_cookies[1] is None
    finally:
        await api.close()
        await runner.cleanup()


def test_gathered_requests_share_one_connection() -> None:
    anyio.run(_gathered_requests_share_one_connection)


def test_api_key_requests_stay_cookie_free() -> None:
    anyio.run(_api_key_requests_stay_cookie_free)
