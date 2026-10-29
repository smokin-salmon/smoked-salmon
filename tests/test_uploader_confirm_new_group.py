"""upload.requests.confirm_new_group: the "upload to an existing group?" question when nothing was found.

Both checks (the group search and the recent-uploads log) ran and found nothing: the question is skipped
and the upload goes to a new group. The tracker gets the same requests with the key on and off.
"""

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon import cfg
from salmon.errors import LoginError, RequestFailedError
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import dupe_checker, recheck_dupe

SEARCHSTRS = dupe_checker.generate_dupe_check_searchstrs([["Artist", "main"]], "Title")


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"

    def __init__(self, base_url: str, cookie: str, api_key: str = "") -> None:
        self.base_url = base_url
        self.cookie = cookie
        self.api_key = api_key
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


class FakeTracker:
    """A local tracker: a group search answering with `results`, and a log.php that needs the cookie "good"."""

    def __init__(self, results: list[dict] | None = None, search_fails: bool = False, logged: bool = False) -> None:
        self.results = results or []
        self.search_fails = search_fails
        self.logged = logged
        self.hits: list[str] = []

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/ajax.php", self.ajax)
        app.router.add_get("/log.php", self.log)
        app.router.add_get("/login.php", self.login)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def ajax(self, request: web.Request) -> web.Response:
        self.hits.append(request.path_qs)
        if self.search_fails:
            return web.json_response({"status": "failure", "error": "bad search"})
        return web.json_response({"status": "success", "response": {"results": self.results}})

    async def log(self, request: web.Request) -> web.Response:
        self.hits.append(request.path_qs)
        if request.cookies.get("session") != "good":
            raise web.HTTPFound("/login.php")
        page = int(request.query["page"])
        text = ""
        if self.logged and page == 1:
            text = '<span class="log_upload"><a href="torrents.php?torrentid=7">7</a> (Artist - Title) (x)</span>'
        return web.Response(text=text, content_type="text/html")

    async def login(self, request: web.Request) -> web.Response:
        self.hits.append(request.path_qs)
        raise web.HTTPFound("/log.php")


def _result(group_id: int) -> dict:
    return {
        "groupId": group_id,
        "groupName": "Title",
        "groupYear": 2020,
        "artist": "Artist",
        "releaseType": "Album",
        "tags": [],
        "torrents": [],
    }


class Asked:
    """Stands in for the terminal prompt, answering "n" (a new group) and counting the questions."""

    def __init__(self) -> None:
        self.count = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> str:
        self.count += 1
        return "n"


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> Asked:
    asked = Asked()
    monkeypatch.setattr(dupe_checker.click, "prompt", asked)
    monkeypatch.setattr(cfg.upload.requests, "check_recent_uploads", True)
    monkeypatch.setattr(cfg.upload.requests, "confirm_new_group", False)
    return asked


async def _check(tracker: FakeTracker, cookie: str = "good", api_key: str = "") -> tuple[int | None, list[str]]:
    api = FakeApi(await tracker.start(), cookie=cookie, api_key=api_key)
    try:
        group_id = await dupe_checker.check_existing_group(api, SEARCHSTRS, our_title="Title")
        return group_id, tracker.hits
    finally:
        await api.close()
        await tracker.runner.cleanup()


def _run(tracker: FakeTracker, **kwargs: Any) -> tuple[int | None, list[str]]:
    async def run() -> tuple[int | None, list[str]]:
        return await _check(tracker, **kwargs)

    return anyio.run(run)


def test_nothing_found_by_either_check_goes_to_a_new_group_without_asking(asked: Asked, capsys) -> None:
    group_id, _ = _run(FakeTracker())

    assert group_id is None
    assert asked.count == 0
    out = capsys.readouterr().out
    assert "No group found on RED: new group" in out


def test_the_tracker_gets_the_same_requests_with_the_key_on_and_off(
    asked: Asked, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, skipped = _run(FakeTracker())
    skipped_asked = asked.count
    monkeypatch.setattr(cfg.upload.requests, "confirm_new_group", True)
    _, confirmed = _run(FakeTracker())

    assert (skipped_asked, asked.count) == (0, 1)
    assert skipped == confirmed
    assert any(hit.startswith("/log.php") for hit in skipped)


def test_with_the_key_on_the_question_is_asked(asked: Asked, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload.requests, "confirm_new_group", True)

    group_id, _ = _run(FakeTracker())

    assert group_id is None
    assert asked.count == 1


def test_a_search_result_is_asked_about(asked: Asked) -> None:
    _run(FakeTracker(results=[_result(5)]))

    assert asked.count == 1


def test_a_recent_upload_match_is_asked_about(asked: Asked) -> None:
    _run(FakeTracker(logged=True))

    assert asked.count == 1


def test_without_the_recent_uploads_check_the_question_is_asked(asked: Asked, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload.requests, "check_recent_uploads", False)

    tracker = FakeTracker()
    _run(tracker)

    assert asked.count == 1
    assert not any(hit.startswith("/log.php") for hit in tracker.hits)


def test_without_a_session_cookie_the_question_is_asked(asked: Asked) -> None:
    tracker = FakeTracker()
    _run(tracker, cookie="", api_key="an-api-key")

    assert asked.count == 1
    assert not any(hit.startswith("/log.php") for hit in tracker.hits)


def _raises(call: Callable[[], Awaitable[Any]], error: type[Exception]) -> None:
    async def run() -> None:
        with pytest.raises(error):
            await call()

    anyio.run(run)


def test_a_failing_log_read_is_not_a_new_group(asked: Asked) -> None:
    """An expired cookie fails the log read: the check raises as it does today, and nothing is skipped."""
    _raises(lambda: _check(FakeTracker(), cookie="expired"), LoginError)

    assert asked.count == 0


def test_a_failing_search_is_not_a_new_group(asked: Asked, capsys) -> None:
    _raises(lambda: _check(FakeTracker(search_fails=True)), RequestFailedError)

    assert asked.count == 0
    out = capsys.readouterr().out
    assert "No group found" not in out


def test_the_resolve_step_asks_when_the_log_was_not_read(asked: Asked) -> None:
    """recent_uploads None (not read) is not an empty log: it is asked about."""

    async def run() -> None:
        api = FakeApi("http://127.0.0.1:1", cookie="good")
        try:
            group_id = await dupe_checker.resolve_existing_group(api, SEARCHSTRS, [], None)
            assert group_id is None
        finally:
            await api.close()

    anyio.run(run)

    assert asked.count == 1


def _recheck(tracker: FakeTracker) -> tuple[int | None, list[str]]:
    """recheck_dupe after a review that changed the title: the search strings it was given no longer fit."""

    async def run() -> tuple[int | None, list[str]]:
        api = FakeApi(await tracker.start(), cookie="good")
        metadata = {"artists": [["Artist", "main"]], "title": "Reviewed Title", "catno": None}
        try:
            return await recheck_dupe(api, SEARCHSTRS, metadata), tracker.hits
        finally:
            await api.close()
            await tracker.runner.cleanup()

    return anyio.run(run)


def test_the_recheck_after_a_review_skips_the_question_when_nothing_is_found(
    asked: Asked, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_checks: list[tuple[list[str], str | None]] = []
    real = dupe_checker.dupe_check_recent_torrents

    async def spy(gazelle_site: BaseGazelleApi, searchstrs: list[str], our_title: str | None = None) -> list[tuple]:
        log_checks.append((list(searchstrs), our_title))
        return await real(gazelle_site, searchstrs, our_title)

    monkeypatch.setattr(dupe_checker, "dupe_check_recent_torrents", spy)

    group_id, hits = _recheck(FakeTracker())

    out = capsys.readouterr().out
    reviewed = dupe_checker.generate_dupe_check_searchstrs([["Artist", "main"]], "Reviewed Title")
    assert log_checks == [(reviewed, "Reviewed Title")]
    assert group_id is None
    assert asked.count == 0
    assert "No group found on RED: new group" in out
    assert any("reviewed" in hit.lower() for hit in hits)
    assert any(hit.startswith("/log.php") for hit in hits)


def test_the_recheck_after_a_review_asks_with_the_key_on(asked: Asked, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload.requests, "confirm_new_group", True)

    group_id, hits = _recheck(FakeTracker())

    assert group_id is None
    assert asked.count == 1
    assert any(hit.startswith("/log.php") for hit in hits)
