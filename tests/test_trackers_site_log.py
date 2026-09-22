import asyncio
import sys
from pathlib import Path

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.errors import LoginError
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import dupe_checker, last_min_dupe_check


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
    """A local tracker whose log.php needs the session cookie "good".

    Without it, log.php redirects to login.php and login.php back to log.php,
    the loop an API-key-only config ran into in #432.
    """

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.requested_when_page_one_answered: list[str] = []

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
        self.hits.append(request.path)
        return web.json_response({"status": "success", "response": {"results": []}})

    async def log(self, request: web.Request) -> web.Response:
        self.hits.append(request.path_qs)
        if request.cookies.get("session") != "good":
            raise web.HTTPFound("/login.php")
        page = request.query["page"]
        if page == "1":
            # Slow enough for pages requested alongside page 1 to arrive before it is answered.
            await asyncio.sleep(0.2)
            self.requested_when_page_one_answered = list(self.hits)
        return web.Response(
            text=f'<span class="log_upload"><a href="torrents.php?torrentid={page}">{page}</a>'
            f" (Artist - Title {page}) (x)</span>",
            content_type="text/html",
        )

    async def login(self, request: web.Request) -> web.Response:
        self.hits.append(request.path)
        raise web.HTTPFound("/log.php")


async def _new_group(*args, **kwargs) -> str:
    return "n"


async def _api_key_only_dupe_check_skips_the_log() -> None:
    tracker = FakeTracker()
    api = FakeApi(await tracker.start(), cookie="", api_key="an-api-key")
    try:
        # A new release: the search finds nothing, which is when the log would be scraped.
        assert await dupe_checker.check_existing_group(api, ["artist title"]) is None
        await last_min_dupe_check(api, ["artist title"])
        assert tracker.hits == ["/ajax.php"]
    finally:
        await api.close()
        await tracker.runner.cleanup()


def test_api_key_only_dupe_check_skips_the_log(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(dupe_checker.click, "prompt", _new_group)
    anyio.run(_api_key_only_dupe_check_skips_the_log)
    assert capsys.readouterr().out.count("needs a session cookie") == 2


async def _expired_cookie_costs_one_request() -> None:
    tracker = FakeTracker()
    api = FakeApi(await tracker.start(), cookie="expired")
    try:
        with pytest.raises(LoginError):
            await api.get_uploads_from_log()
        assert tracker.hits == ["/log.php?page=1"]
    finally:
        await api.close()
        await tracker.runner.cleanup()


def test_expired_cookie_costs_one_request() -> None:
    anyio.run(_expired_cookie_costs_one_request)


async def _valid_cookie_reads_every_page_after_the_first() -> None:
    tracker = FakeTracker()
    api = FakeApi(await tracker.start(), cookie="good")
    try:
        uploads = await api.get_uploads_from_log()
        assert tracker.requested_when_page_one_answered == ["/log.php?page=1"]
        assert sorted(tracker.hits) == [f"/log.php?page={page}" for page in range(1, 10)]
        assert sorted(uploads) == [(str(page), "Artist", f"Title {page}") for page in range(1, 10)]
    finally:
        await api.close()
        await tracker.runner.cleanup()


def test_valid_cookie_reads_every_page_after_the_first() -> None:
    anyio.run(_valid_cookie_reads_every_page_after_the_first)
