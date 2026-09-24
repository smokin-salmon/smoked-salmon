from urllib.parse import unquote_plus

import anyio
from aiohttp import web
from aiolimiter import AsyncLimiter

from salmon.trackers.base import BaseGazelleApi, _normalize_session_cookie

DECODED_COOKIE = "NYzc/MwZ+4rK:Jcc5R/l9nvCJpY8hI7uKpA=="


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    cookie = DECODED_COOKIE

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        super().__init__()
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


def test_normalize_session_cookie_encodes_decoded_red_style_values() -> None:
    decoded = (
        "NYzc/MwZxhjA8VKzFuyi0HcVN/TiaCiwgOAx+4rKhpWQsNLtfImjNY1xJzForZ3UUP+uo66wrrv7J6V5OrXnsb+Q"
        "kvCP6H6bXMJ2jqElXZJf+xMfryUSE4pocW8IaeRk:Jcc5R/l9nvCJpY8hI7uKpA=="
    )

    assert _normalize_session_cookie(decoded) == (
        "NYzc%2FMwZxhjA8VKzFuyi0HcVN%2FTiaCiwgOAx%2B4rKhpWQsNLtfImjNY1xJzForZ3UUP%2Buo66wrrv7J6V5"
        "OrXnsb%2BQkvCP6H6bXMJ2jqElXZJf%2BxMfryUSE4pocW8IaeRk%3AJcc5R%2Fl9nvCJpY8hI7uKpA%3D%3D"
    )


def test_normalize_session_cookie_is_idempotent_for_encoded_values() -> None:
    encoded = (
        "NYzc%2FMwZxhjA8VKzFuyi0HcVN%2FTiaCiwgOAx%2B4rKhpWQsNLtfImjNY1xJzForZ3UUP%2Buo66wrrv7J6V5"
        "OrXnsb%2BQkvCP6H6bXMJ2jqElXZJf%2BxMfryUSE4pocW8IaeRk%3AJcc5R%2Fl9nvCJpY8hI7uKpA%3D%3D"
    )

    assert _normalize_session_cookie(encoded) == encoded


async def _decoded_cookie_reaches_php_intact() -> None:
    sent_cookies = []

    async def handle_ajax(request: web.Request) -> web.Response:
        sent_cookies.append(request.headers.get("Cookie"))
        return web.json_response({"status": "success", "response": {"authkey": "a", "passkey": "p"}})

    app = web.Application()
    app.router.add_get("/ajax.php", handle_ajax)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}")
    try:
        await api._request("GET", api.base_url + "/ajax.php", params={"action": "index"})
        name, _, value = sent_cookies[0].partition("=")
        assert name == "session"
        assert '"' not in value
        # PHP url-decodes $_COOKIE, so this is the session Gazelle looks up.
        assert unquote_plus(value) == DECODED_COOKIE
    finally:
        await api.close()
        await runner.cleanup()


def test_decoded_cookie_reaches_php_intact() -> None:
    anyio.run(_decoded_cookie_reaches_php_intact)
