import sys
from datetime import datetime
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


def _row(torrent_id: int, title: str, time: str | None) -> str:
    """A site log row as OPS writes it (#826): its time, then the upload, which links to its torrent."""
    when = f'<td><span class="time tooltip" title="{time}">some minutes ago</span></td>' if time is not None else ""
    return (
        f"<tr>{when}<td>"
        f'<span class="log-uploaded">Torrent <a href="torrents.php?torrentid={torrent_id}">{torrent_id}</a> '
        f'(Artist \u2013 {title}) (110.97 MiB) was uploaded by <a href="user.php?id=1">someone</a></span>'
        "</td></tr>"
    )


def _page(times: list[str | None], first_id: int, titles: dict[int, str]) -> str:
    ids = range(first_id, first_id + len(times))
    rows = "".join(_row(id_, titles.get(id_, f"Title {id_}"), time) for id_, time in zip(ids, times, strict=True))
    return f"<html><body><table>{rows}</table></body></html>"


def _minutes(newest: str, count: int) -> list[str | None]:
    """The times of `count` rows one minute apart, from `newest` ("HH:MM") down, on 2026-10-30."""
    hour, minute = (int(part) for part in newest.split(":"))
    start = hour * 60 + minute
    return [f"2026-10-30 {(start - i) // 60:02d}:{(start - i) % 60:02d}" for i in range(count)]


class FakeTracker:
    """A local tracker whose log.php needs the session cookie "good".

    Without it, log.php redirects to login.php and login.php back to log.php,
    the loop an API-key-only config ran into in #432. With it, page N holds the rows
    whose times are `pages[N]` (a time of None: a row with none), and a page not given has no rows.
    Row i of page N is the upload of torrent 100 * N + i, titled "Title <id>" unless `titles` names it.
    """

    def __init__(self, pages: dict[int, list[str | None]] | None = None, titles: dict[int, str] | None = None) -> None:
        self.hits: list[str] = []
        self.pages = pages or {}
        self.titles = titles or {}

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
        page = int(request.query["page"])
        return web.Response(text=_page(self.pages.get(page, []), page * 100, self.titles), content_type="text/html")

    def pages_read(self) -> list[int]:
        return [int(hit.rsplit("=", 1)[1]) for hit in self.hits if hit.startswith("/log.php?page=")]

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


async def _read_log(tracker: FakeTracker) -> list:
    api = FakeApi(await tracker.start(), cookie="good")
    try:
        return await api.get_uploads_from_log()
    finally:
        await api.close()
        await tracker.runner.cleanup()


def _uploads(page: int, count: int) -> list[tuple[str, str, str]]:
    return [(str(page * 100 + i), "Artist", f"Title {page * 100 + i}") for i in range(count)]


def test_a_first_page_spanning_18_minutes_is_the_only_page_read() -> None:
    # RED's and OPS's page 1 spans about 18 minutes, and a new upload reaches the search within 1 to 2.
    tracker = FakeTracker({1: _minutes("22:55", 19), 2: _minutes("22:36", 19)})
    uploads = anyio.run(_read_log, tracker)
    assert tracker.pages_read() == [1]
    assert uploads == _uploads(1, 19)


def test_a_first_page_spanning_exactly_the_window_is_the_only_page_read() -> None:
    tracker = FakeTracker({1: _minutes("22:55", 6), 2: _minutes("22:49", 19)})
    anyio.run(_read_log, tracker)
    assert tracker.pages_read() == [1]


def test_a_first_page_spanning_2_minutes_reads_the_second() -> None:
    # Page 1 spans 22:55 to 22:53, page 2 takes the rows read back to 22:49: past the window.
    tracker = FakeTracker({1: _minutes("22:55", 3), 2: _minutes("22:52", 4), 3: _minutes("22:48", 19)})
    uploads = anyio.run(_read_log, tracker)
    assert tracker.pages_read() == [1, 2]
    assert uploads == _uploads(1, 3) + _uploads(2, 4)


def test_three_short_pages_stop_at_the_third() -> None:
    tracker = FakeTracker({page: _minutes(f"22:{56 - page}", 2) for page in range(1, 5)})
    uploads = anyio.run(_read_log, tracker)
    assert tracker.pages_read() == [1, 2, 3]
    assert uploads == _uploads(1, 2) + _uploads(2, 2) + _uploads(3, 2)


@pytest.mark.parametrize("time", [None, "", "17 minutes ago", "Oct 30 2026, 22:55"])
def test_a_page_with_no_readable_time_reads_the_next(time: str | None) -> None:
    tracker = FakeTracker({1: [time] * 3, 2: _minutes("22:40", 19), 3: _minutes("22:21", 19)})
    uploads = anyio.run(_read_log, tracker)
    assert tracker.pages_read() == [1, 2]
    assert uploads == _uploads(1, 3) + _uploads(2, 19)


def test_a_log_with_no_readable_time_stops_at_the_third_page() -> None:
    tracker = FakeTracker({page: [None, "17 minutes ago"] for page in range(1, 5)})
    anyio.run(_read_log, tracker)
    assert tracker.pages_read() == [1, 2, 3]


def test_log_times_reads_every_row_not_only_uploads() -> None:
    page = (
        '<table><tr><td><span class="time tooltip" title="2026-10-30 22:55">17 minutes ago</span></td>'
        "<td>Torrent 1 (Artist \u2013 Title) was deleted by someone</td></tr>"
        f"{_row(2, 'Title', '2026-10-30 22:37')}"
        f"{_row(3, 'Title', 'not a time')}"
        '<tr><td><span class="time tooltip">no title</span></td></tr></table>'
    )
    assert _parser().log_times(page) == [datetime(2026, 10, 30, 22, 55), datetime(2026, 10, 30, 22, 37)]


async def _recent_upload_is_found(tracker: FakeTracker, title: str) -> list:
    api = FakeApi(await tracker.start(), cookie="good")
    try:
        searchstrs = dupe_checker.generate_dupe_check_searchstrs([["Artist", "main"]], title)
        return await dupe_checker.dupe_check_recent_torrents(api, searchstrs, title)
    finally:
        await api.close()
        await tracker.runner.cleanup()


def test_a_matching_upload_on_the_first_page_is_found() -> None:
    title = "Moonlight Sonatas"
    tracker = FakeTracker({1: _minutes("22:55", 19)}, titles={105: title})
    hits = anyio.run(_recent_upload_is_found, tracker, title)
    assert hits == [("105", "Artist", title)]
    assert tracker.pages_read() == [1]


OPS_ROW = (
    '<tr class="rowb" id="log_10287529"><td>'
    '<span class="log-uploaded">Torrent <a href="torrents.php?torrentid=3844069">3844069</a> '
    "(OutKast \u2013 ATLiens [1996 Album]) (110.97 MiB) was uploaded by "
    '<a href="user.php?id=1">someone</a></span></td></tr>'
)


def _parser() -> FakeApi:
    return FakeApi("http://127.0.0.1:1", cookie="")


def test_ops_log_row_is_parsed() -> None:
    assert _parser().parse_uploads_from_log_html(OPS_ROW) == [("3844069", "OutKast", "ATLiens [1996 Album]")]


def test_log_upload_row_with_a_hyphen_is_parsed() -> None:
    row = '<span class="log_upload"><a href="torrents.php?torrentid=7">7</a> (Artist - Title) (x)</span>'
    assert _parser().parse_uploads_from_log_html(row) == [("7", "Artist", "Title")]


def test_en_dash_wins_over_a_hyphen_in_the_title() -> None:
    row = '<span class="log-uploaded"><a href="torrents.php?torrentid=7">7</a> (Artist \u2013 Title - Remix) (x)</span>'
    assert _parser().parse_uploads_from_log_html(row) == [("7", "Artist", "Title - Remix")]


def test_en_dash_row_splits_once() -> None:
    row = (
        '<span class="log-uploaded"><a href="torrents.php?torrentid=7">7</a> '
        "(Artist \u2013 Title \u2013 Subtitle) (x)</span>"
    )
    assert _parser().parse_uploads_from_log_html(row) == [("7", "Artist", "Title \u2013 Subtitle")]


def test_hyphen_row_with_an_en_dash_in_the_title() -> None:
    row = (
        '<span class="log_upload"><a href="torrents.php?torrentid=7">7</a> (Artist - Title \u2013 Subtitle) (x)</span>'
    )
    assert _parser().parse_uploads_from_log_html(row) == [("7", "Artist", "Title \u2013 Subtitle")]
