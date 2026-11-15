import json
from typing import Any

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

from salmon.errors import LoginError
from salmon.trackers.base import BaseGazelleApi


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


def _look_up(answer: Any, api_key: str = "an-api-key") -> tuple[int | None, list[tuple]]:
    """Ask a fake tracker, which gives *answer* on every page, for the group of torrent 7; and what it was sent."""
    sent: list[tuple] = []

    async def page(request: web.Request) -> web.Response:
        sent.append(
            (request.path, dict(request.query), request.headers.get("Cookie"), request.headers.get("Authorization"))
        )
        if isinstance(answer, web.HTTPException):
            raise answer
        if isinstance(answer, str):
            return web.Response(text=answer, content_type="text/html")
        return web.json_response(answer)

    async def run() -> int | None:
        app = web.Application()
        app.router.add_route("*", "/{page}.php", page)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}")
        api.api_key = api_key
        try:
            return await api.torrent_group_id(7)
        finally:
            await api.close()
            await runner.cleanup()

    return anyio.run(run), sent


TORRENT = {"status": "success", "response": {"group": {"id": 2, "name": "Album"}, "torrent": {"id": 7}}}


@pytest.mark.parametrize(
    ("api_key", "cookie", "authorization"),
    [("an-api-key", None, "an-api-key"), ("", "session=a-cookie", None)],
    ids=["api key", "cookie"],
)
def test_the_group_comes_from_one_api_lookup(api_key: str, cookie: str | None, authorization: str | None) -> None:
    group_id, sent = _look_up(TORRENT, api_key)
    assert group_id == 2
    assert sent == [("/ajax.php", {"action": "torrent", "id": "7"}, cookie, authorization)]


FAILURE = {"status": "failure", "error": "bad parameters"}


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (FAILURE, "bad parameters"),
        (web.HTTPBadRequest(text=json.dumps(FAILURE), content_type="application/json"), "bad parameters"),
        (web.HTTPNotFound(text="<html>404</html>"), "<html>404</html>"),
        ("<html>Down for maintenance</html>", "Down for maintenance"),
    ],
    ids=["failure status", "failure status with HTTP 400", "HTTP error", "not JSON"],
)
def test_a_lookup_the_tracker_refuses_names_no_group(capsys, answer: Any, reason: str) -> None:
    # Any refusal is None, with the reason: RED's answer for a missing torrent is not known.
    group_id, sent = _look_up(answer)
    out = capsys.readouterr().out
    assert group_id is None
    assert len(sent) == 1
    assert "Could not look up the group of torrent 7 on RED: " in out
    assert reason in out


@pytest.mark.parametrize(
    "response",
    [{"torrent": {"id": 7}}, {"group": {"id": 0}}, {"group": {"id": "two"}}, {"group": [2]}, [2]],
    ids=["no group", "group 0", "id not a number", "group not an object", "answer not an object"],
)
def test_an_answer_without_a_group_id_names_no_group(capsys, response: Any) -> None:
    group_id, sent = _look_up({"status": "success", "response": response})
    out = capsys.readouterr().out
    assert group_id is None
    assert len(sent) == 1
    assert "RED's answer for torrent 7 names no group." in out


def test_a_lookup_sent_to_the_login_page_raises() -> None:
    with pytest.raises(LoginError):
        _look_up(web.HTTPFound("/login.php"))
