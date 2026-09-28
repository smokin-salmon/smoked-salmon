"""Tracker secrets never reach salmon's output, debug output included (#501).

Every request goes to a local fake tracker.
"""

import contextlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon import cfg
from salmon.common import UploadFiles
from salmon.errors import RequestError
from salmon.trackers.base import BaseGazelleApi

AUTHKEY = "0123456789abcdef0123456789abcdef"
PASSKEY = "fedcba9876543210fedcba9876543210"
# Written decoded in the config, as some cookie editors show it; sent percent-encoded.
COOKIE = "UNIQUECOOKIE/part+two=="
API_KEY = "UNIQUEAPIKEY.0123456789"
SECRETS = [AUTHKEY, PASSKEY, "UNIQUECOOKIE", API_KEY, "NEWSESSION"]

# What Gazelle pages carry: links with the authkey and passkey, forms with the authkey, the
# authkey in a script, and the announce URL with the passkey.
PAGE = f"""<html><head><script>var authkey = "{AUTHKEY}";</script></head><body>
<a href="torrents.php?action=download&amp;id=1&amp;authkey={AUTHKEY}&amp;torrent_pass={PASSKEY}">DL</a>
<a href="logout.php?auth={AUTHKEY}">Logout</a>
<form><input type="hidden" name="auth" value="{AUTHKEY}" /><input value='{AUTHKEY}' name='auth'></form>
<input type="text" value="https://flacsfor.me/{PASSKEY}/announce" size="80" />
<p>Group page for Artist - Album</p>
</body></html>"""


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = COOKIE
    tracker_url = "https://flacsfor.me"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    handler: Handler,
    call: Callable[[FakeApi], Awaitable[object]],
    *,
    debug: bool = True,
    known: bool = False,
) -> str:
    """Send `call` to a fake tracker answering with `handler`; return what salmon printed and raised.

    Args:
        known: Whether the client already has its authkey and passkey, as it does once authenticated.
    """
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", debug)
    raised: list[str] = []

    async def main() -> None:
        app = web.Application()
        app.router.add_route("*", "/{page}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}")
        api.api_key = API_KEY
        if known:
            api.authkey, api.passkey = AUTHKEY, PASSKEY
        try:
            await call(api)
        except RequestError as err:
            raised.append(str(err))
        finally:
            await api.close()
            await runner.cleanup()

    anyio.run(main)
    out, err = capsys.readouterr()
    return "\n".join([out, err, *raised])


def _get(path: str, **params: str) -> Callable[[FakeApi], Awaitable[object]]:
    return lambda api: api._request("GET", f"{api.base_url}/{path}", params=params or None)


async def _page(request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


def _assert_no_secret(output: str) -> None:
    assert not [secret for secret in SECRETS if secret in output], output


def test_a_page_in_the_debug_output_keeps_its_authkey_and_passkey_hidden(monkeypatch, capsys) -> None:
    # Not authenticated: the authkey and passkey are not known yet, the page's shapes give them away.
    output = _run(monkeypatch, capsys, _page, _get("torrents.php", id="1"))

    assert "[DEBUG] GET http://127.0.0.1:" in output
    assert "[DEBUG] status: 200" in output
    assert "Group page for Artist - Album" in output
    assert "torrents.php?action=download&amp;id=1&amp;authkey=[REDACTED]&amp;torrent_pass=[REDACTED]" in output
    assert '<input type="hidden" name="auth" value="[REDACTED]" />' in output
    assert "https://flacsfor.me/[REDACTED]/announce" in output
    _assert_no_secret(output)


def test_a_session_cookie_the_tracker_sets_is_hidden(monkeypatch, capsys) -> None:
    async def handler(request: web.Request) -> web.Response:
        response = web.json_response({"status": "success", "response": {}})
        response.headers["Set-Cookie"] = "session=NEWSESSION; path=/; secure; HttpOnly"
        return response

    output = _run(monkeypatch, capsys, handler, _get("ajax.php", action="index"))

    assert "[DEBUG] response headers:" in output
    # The cookie's name and attributes stay: they tell a reset or expired session apart.
    assert "session=[REDACTED]; path=/; secure; HttpOnly" in output
    _assert_no_secret(output)


@pytest.mark.parametrize(
    "echoed",
    [COOKIE, "UNIQUECOOKIE%2Fpart%2Btwo%3D%3D", API_KEY, AUTHKEY, PASSKEY],
    ids=["session cookie", "session cookie as sent", "api key", "authkey", "passkey"],
)
def test_a_configured_or_fetched_secret_anywhere_in_a_body_is_hidden(monkeypatch, capsys, echoed: str) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=f"Hello, your key is {echoed} today")

    output = _run(monkeypatch, capsys, handler, _get("index.php"), known=True)

    assert "Hello, your key is [REDACTED] today" in output
    _assert_no_secret(output)


def test_an_index_answer_keeps_its_authkey_and_passkey_hidden(monkeypatch, capsys) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "success", "response": {"username": "dean", "authkey": AUTHKEY, "passkey": PASSKEY}}
        )

    output = _run(monkeypatch, capsys, handler, _get("ajax.php", action="index"))

    assert '"username": "dean"' in output
    _assert_no_secret(output)


def test_a_redirect_carrying_the_authkey_is_hidden(monkeypatch, capsys) -> None:
    async def handler(request: web.Request) -> web.Response:
        if request.path == "/upload.php":
            raise web.HTTPFound(f"/torrents.php?id=5&authkey={AUTHKEY}")
        return web.Response(text="group page")

    output = _run(monkeypatch, capsys, handler, _get("upload.php"))

    # The hop is printed with its path and query, the secret value masked.
    assert "/torrents.php?id=5&authkey=[REDACTED]" in output
    _assert_no_secret(output)


def test_the_request_params_keep_the_authkey_hidden(monkeypatch, capsys) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    output = _run(monkeypatch, capsys, handler, _get("torrents.php", action="download", id="1", torrent_pass=PASSKEY))

    assert '"action":"download"' in output
    _assert_no_secret(output)


def test_an_error_page_is_printed_without_its_secrets(monkeypatch, capsys) -> None:
    # Printed and raised whether or not debug output is on.
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=404, text=PAGE, content_type="text/html")

    output = _run(monkeypatch, capsys, handler, _get("torrents.php", id="1"), debug=False)

    assert "Request to RED failed (404)" in output
    _assert_no_secret(output)


def test_an_api_call_answered_with_a_page_raises_without_its_secrets(monkeypatch, capsys) -> None:
    output = _run(monkeypatch, capsys, _page, lambda api: api.api_call("torrentgroup", {"id": "1"}), debug=False)

    assert "Group page for Artist - Album" in output
    _assert_no_secret(output)


@pytest.mark.parametrize("api_key", [True, False], ids=["api key upload", "site page upload"])
def test_a_failed_upload_shows_the_page_without_its_secrets(monkeypatch, capsys, api_key: bool) -> None:
    async def upload(api: FakeApi) -> None:
        if not api_key:
            api.api_key = ""
        # An api key upload that gets no JSON back prints the answer, then aborts.
        with contextlib.suppress(click.Abort):
            await api.upload({"title": "Album"}, UploadFiles(torrent_data=b"torrent"))

    output = _run(monkeypatch, capsys, _page, upload, debug=False, known=True)

    assert "Group page for Artist - Album" in output
    _assert_no_secret(output)
