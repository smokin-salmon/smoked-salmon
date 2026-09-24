"""What print_torrents (dupe_checker) and _confirm_request_id (request_checker) print and do when
fetching a group or request fails.

An unknown ID gives RequestFailedError, and only that case says "does not exist". A network
failure (retried 5xx) or a login problem (401) must not: on master they wrongly print "does not
exist" too, which could make a user believe a group is gone and upload a duplicate.
"""

import sys
from pathlib import Path

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from tenacity import wait_none

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader.dupe_checker import print_torrents
from salmon.uploader.request_checker import _confirm_request_id


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.api_key = ""
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting, so the tests count attempts, not seconds."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


async def _serve(handler) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_get("/ajax.php", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


def _failure_response(status: int) -> web.Response:
    return web.json_response({"status": "failure", "error": "bad id parameter"}, status=status)


def _server_error(request: web.Request) -> web.Response:
    return web.Response(status=503, text="Service Unavailable")


def _unauthorized(request: web.Request) -> web.Response:
    return web.json_response({"status": "failure", "error": "bad api key"}, status=401)


async def _group_unknown_id_says_does_not_exist(status: int, capsys: pytest.CaptureFixture) -> None:
    hits = []

    async def ajax(request: web.Request) -> web.Response:
        hits.append(request.query["action"])
        return _failure_response(status)

    runner, url = await _serve(ajax)
    api = FakeApi(url)
    try:
        with pytest.raises(click.exceptions.Abort):
            await print_torrents(api, 5)
        out = capsys.readouterr().out
        assert "5 does not exist on RED" in out
        assert "bad id parameter" in out
        assert hits == ["torrentgroup"]
    finally:
        await api.close()
        await runner.cleanup()


async def _group_network_failure_says_could_not_fetch(capsys: pytest.CaptureFixture) -> None:
    hits = []

    async def ajax(request: web.Request) -> web.Response:
        hits.append(request.query["action"])
        return _server_error(request)

    runner, url = await _serve(ajax)
    api = FakeApi(url)
    try:
        with pytest.raises(click.exceptions.Abort):
            await print_torrents(api, 5)
        out = capsys.readouterr().out
        assert "does not exist" not in out
        assert "Could not fetch group 5 from RED" in out
        assert len(hits) == 5
    finally:
        await api.close()
        await runner.cleanup()


async def _group_login_failure_says_could_not_fetch(capsys: pytest.CaptureFixture) -> None:
    hits = []

    async def ajax(request: web.Request) -> web.Response:
        hits.append(request.query["action"])
        return _unauthorized(request)

    runner, url = await _serve(ajax)
    api = FakeApi(url)
    try:
        with pytest.raises(click.exceptions.Abort):
            await print_torrents(api, 5)
        out = capsys.readouterr().out
        assert "does not exist" not in out
        assert "Could not fetch group 5 from RED" in out
        assert hits == ["torrentgroup"]
    finally:
        await api.close()
        await runner.cleanup()


async def _request_unknown_id_says_does_not_exist(status: int, capsys: pytest.CaptureFixture) -> None:
    hits = []

    async def ajax(request: web.Request) -> web.Response:
        hits.append(request.query["action"])
        return _failure_response(status)

    runner, url = await _serve(ajax)
    api = FakeApi(url)
    try:
        with pytest.raises(click.exceptions.Abort):
            await _confirm_request_id(api, 7)
        out = capsys.readouterr().out
        assert "7 does not exist on RED" in out
        assert "bad id parameter" in out
        assert hits == ["request"]
    finally:
        await api.close()
        await runner.cleanup()


async def _request_network_failure_says_could_not_fetch(capsys: pytest.CaptureFixture) -> None:
    hits = []

    async def ajax(request: web.Request) -> web.Response:
        hits.append(request.query["action"])
        return _server_error(request)

    runner, url = await _serve(ajax)
    api = FakeApi(url)
    try:
        with pytest.raises(click.exceptions.Abort):
            await _confirm_request_id(api, 7)
        out = capsys.readouterr().out
        assert "does not exist" not in out
        assert "Could not fetch request 7 from RED" in out
        assert len(hits) == 5
    finally:
        await api.close()
        await runner.cleanup()


async def _request_login_failure_says_could_not_fetch(capsys: pytest.CaptureFixture) -> None:
    hits = []

    async def ajax(request: web.Request) -> web.Response:
        hits.append(request.query["action"])
        return _unauthorized(request)

    runner, url = await _serve(ajax)
    api = FakeApi(url)
    try:
        with pytest.raises(click.exceptions.Abort):
            await _confirm_request_id(api, 7)
        out = capsys.readouterr().out
        assert "does not exist" not in out
        assert "Could not fetch request 7 from RED" in out
        assert hits == ["request"]
    finally:
        await api.close()
        await runner.cleanup()


def test_group_unknown_id_200_says_does_not_exist(capsys: pytest.CaptureFixture) -> None:
    anyio.run(_group_unknown_id_says_does_not_exist, 200, capsys)


def test_group_unknown_id_400_says_does_not_exist(capsys: pytest.CaptureFixture) -> None:
    anyio.run(_group_unknown_id_says_does_not_exist, 400, capsys)


def test_group_network_failure_says_could_not_fetch(capsys: pytest.CaptureFixture) -> None:
    anyio.run(_group_network_failure_says_could_not_fetch, capsys)


def test_group_login_failure_says_could_not_fetch(capsys: pytest.CaptureFixture) -> None:
    anyio.run(_group_login_failure_says_could_not_fetch, capsys)


def test_request_unknown_id_200_says_does_not_exist(capsys: pytest.CaptureFixture) -> None:
    anyio.run(_request_unknown_id_says_does_not_exist, 200, capsys)


def test_request_unknown_id_400_says_does_not_exist(capsys: pytest.CaptureFixture) -> None:
    anyio.run(_request_unknown_id_says_does_not_exist, 400, capsys)


def test_request_network_failure_says_could_not_fetch(capsys: pytest.CaptureFixture) -> None:
    anyio.run(_request_network_failure_says_could_not_fetch, capsys)


def test_request_login_failure_says_could_not_fetch(capsys: pytest.CaptureFixture) -> None:
    anyio.run(_request_login_failure_says_could_not_fetch, capsys)
