"""Helpers of tests/test_checkconf_connection.py that other test modules use too."""

import asyncio
import time
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import anyio
import pytest
from aiohttp import web
from asyncclick.testing import CliRunner

import salmon.trackers
from salmon.commands import checkconf
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
