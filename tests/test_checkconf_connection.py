"""What ``salmon checkconf -t`` prints and sends for a tracker, against a local fake (#652).

The expected output is what the command printed before its tracker part moved to ``trackers.connection``. The
tracker is a fake on 127.0.0.1; the network guard in conftest.py stands.
"""

import asyncio
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import anyio
import pytest
from aiohttp import web
from asyncclick.testing import CliRunner

import salmon.trackers
from salmon import cfg
from salmon.commands import checkconf
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi

Sent = tuple[str, str]


class FakeTracker:
    """A local Gazelle tracker answering the index call, which records who asked: ``cookie`` or ``api key``.

    Attributes:
        error: What a refused request says.
        rate_limited: The first requests this many are answered 429, asking to wait a second.
        hold: Requests wait for this to be set before they are answered.
    """

    def __init__(
        self,
        *,
        cookie_ok: bool = True,
        key_ok: bool = True,
        error: str = "bad credentials",
        rate_limited: int = 0,
        hold: asyncio.Event | None = None,
    ) -> None:
        self.cookie_ok = cookie_ok
        self.key_ok = key_ok
        self.error = error
        self.rate_limited = rate_limited
        self.hold = hold
        self.sent: list[Sent] = []
        self.times: list[float] = []
        self.url = ""

    async def _handle(self, request: web.Request) -> web.Response:
        by_key = "Authorization" in request.headers
        assert not (by_key and "Cookie" in request.headers), "a request carried both the key and the cookie"
        self.sent.append((request.query.get("action", ""), "api key" if by_key else "cookie"))
        self.times.append(time.monotonic())
        if self.hold is not None:
            await self.hold.wait()
        if len(self.sent) <= self.rate_limited:
            return web.json_response({"status": "failure"}, status=429, headers={"Retry-After": "1"})
        if not (self.key_ok if by_key else self.cookie_ok):
            return web.json_response({"status": "failure", "error": self.error}, status=403)
        return web.json_response({"status": "success", "response": {"authkey": "authkey", "passkey": "passkey"}})

    @asynccontextmanager
    async def serving(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            yield self
        finally:
            await runner.cleanup()


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_learned_secrets", set())


def point_at(monkeypatch: pytest.MonkeyPatch, fakes: dict[str, FakeTracker]) -> None:
    """Make every tracker client a client of the fake for its site code."""

    def client(code: str) -> BaseGazelleApi:
        site = salmon.trackers.tracker_classes[code]()
        site.base_url = fakes[code].url
        return site

    monkeypatch.setattr(salmon.trackers, "get_class", lambda code: partial(client, code))


def run_checkconf(fake: FakeTracker, *args: str) -> Any:
    async def run() -> Any:
        async with fake.serving():
            return await CliRunner().invoke(checkconf, list(args))

    return anyio.run(run)


@pytest.fixture
def red(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    """RED alone in the config, with the API key given (or none)."""
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])

    def configure(api_key: str | None) -> None:
        assert cfg.tracker.red is not None
        monkeypatch.setattr(cfg.tracker.red, "api_key", api_key)

    return configure


def shown(output: str, fake: FakeTracker) -> str:
    """The output, with what changes from run to run fixed: the fake's port and the response headers."""
    output = output.replace(fake.url, "http://127.0.0.1:PORT")
    return re.sub(r"(\[DEBUG\] response headers: ).*", r"\1HEADERS", output)


def request_dump(*, by_key: bool, status: int, body: str) -> str:
    return (
        "[DEBUG] GET http://127.0.0.1:PORT/ajax.php\n"
        '[DEBUG] params: {"action":"index"}\n'
        f"[DEBUG] use_api_key: {by_key}\n"
        f"[DEBUG] status: {status}\n"
        "[DEBUG] response headers: HEADERS\n"
        f"[DEBUG] response body: {body}\n"
    )


INDEX_BODY = '{"status": "success", "response": {"authkey": "[REDACTED]", "passkey": "[REDACTED]"}}'
REFUSED_BODY = '{"status": "failure", "error": "bad credentials"}'
HEADER = "\n[ Testing Tracker: RED ]\n\n[ Testing Session Cookie ]\n"
LINE = "--------------------------------------------------\n"
COOKIE_OK = request_dump(by_key=False, status=200, body=INDEX_BODY) + "  ✔ Session cookie OK\n"
COOKIE_REFUSED = (
    request_dump(by_key=False, status=403, body=REFUSED_BODY)
    + 'Request to RED failed (403): "bad credentials"\n'
    + '  ✖ Session cookie check failed: "bad credentials"\n'
)
KEY_OK = request_dump(by_key=True, status=200, body=INDEX_BODY) + "  ✔ API authentication OK\n"
KEY_REFUSED = (
    request_dump(by_key=True, status=403, body=REFUSED_BODY)
    + 'Request to RED failed (403): "bad credentials"\n'
    + '  ✖ API authentication failed: "bad credentials"\n'
)


@pytest.mark.parametrize(
    ("key", "fake", "output", "sent"),
    [
        (
            "an-api-key",
            FakeTracker(),
            HEADER + COOKIE_OK + KEY_OK + "\n✔ Successfully checked RED\n" + LINE,
            [("index", "cookie"), ("index", "api key")],
        ),
        (
            None,
            FakeTracker(),
            HEADER + COOKIE_OK + "\n✔ Successfully checked RED\n" + LINE,
            [("index", "cookie")],
        ),
        (
            "an-api-key",
            FakeTracker(key_ok=False),
            HEADER + COOKIE_OK + KEY_REFUSED + "\n✖ Error testing RED (API key)\n" + LINE,
            [("index", "cookie"), ("index", "api key")],
        ),
        (
            "an-api-key",
            FakeTracker(cookie_ok=False),
            HEADER + COOKIE_REFUSED + KEY_OK + "\n✖ Error testing RED (session cookie)\n" + LINE,
            [("index", "cookie"), ("index", "api key")],
        ),
        (
            "an-api-key",
            FakeTracker(cookie_ok=False, key_ok=False),
            HEADER + COOKIE_REFUSED + KEY_REFUSED + "\n✖ Error testing RED (session cookie, API key)\n" + LINE,
            [("index", "cookie"), ("index", "api key")],
        ),
    ],
    ids=["both ok", "no key", "key refused", "cookie refused", "both refused"],
)
def test_checkconf_prints_what_it_printed_before(
    red: Callable[[str | None], None],
    monkeypatch: pytest.MonkeyPatch,
    key: str | None,
    fake: FakeTracker,
    output: str,
    sent: list[Sent],
) -> None:
    red(key)
    point_at(monkeypatch, {"RED": fake})
    # checkconf prints the request dumps whatever the config says.
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", False)

    result = run_checkconf(fake, "-t", "RED")

    assert result.exit_code == 0
    assert shown(result.output, fake) == output
    assert fake.sent == sent


@pytest.mark.parametrize("configured", [False, True])
def test_checkconf_leaves_the_debug_setting_as_the_config_has_it(
    red: Callable[[str | None], None], monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    red("an-api-key")
    fake = FakeTracker()
    point_at(monkeypatch, {"RED": fake})
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", configured)

    result = run_checkconf(fake, "-t", "RED")

    assert "[DEBUG] GET" in result.output
    assert cfg.upload.debug_tracker_connection is configured
