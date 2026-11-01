"""An upload the tracker stored stands, whatever the answer that comes after it.

Gazelle stores the torrent, then answers: with a redirect to the group, to the fill of the request the upload
names, or with a page of its own. A fill the tracker refuses, a group it is not asked for and an answer salmon
cannot read must not report the upload failed: it would go unseeded, and be uploaded again.

Each test runs a local fake tracker and counts the requests it gets.
"""

import sys
import time
from pathlib import Path

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from tenacity import wait_none
from torf import Torrent

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.common import UploadFiles
from salmon.errors import RequestError, UnknownOutcomeError
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi, _query_id

AUTHKEY = "authkey-0123456789abcdef"
PASSKEY = "passkey-0123456789abcdef"
TORRENT_ID = 42
GROUP_ID = 5
REQUEST_ID = 7


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"
    tracker_url = "https://tracker.invalid"

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = base_url
        self.api_key = api_key
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True
        self.authkey = AUTHKEY
        self.passkey = PASSKEY


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry, and look a lost upload up, without waiting."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]
    monkeypatch.setattr(base, "_LOST_UPLOAD_FIRST_WAIT", 0)
    monkeypatch.setattr(base, "_LOST_UPLOAD_SECOND_WAIT", 0)


def _upload_files(tmp_path: Path) -> tuple[UploadFiles, str]:
    album = tmp_path / "album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"not really audio" * 64)
    torrent = Torrent(album, trackers=["http://tracker.invalid/announce"], private=True, source="RED")
    torrent.generate()
    return UploadFiles(torrent_data=torrent.dump()), torrent.infohash.upper()


def _error_page(message: str) -> str:
    """The page Gazelle's error() renders for a message (WhatCD's sections/error/index.php), with status 200."""
    return (
        '<html><body><div class="thin"><div class="header"><h2>Error</h2></div>'
        f'<div class="box pad"><p>{message}</p></div></div>'
        f'<a href="logout.php?auth={AUTHKEY}">Logout</a></body></html>'
    )


def _filled_request_page(torrent_id: int) -> str:
    """A request's page once filled: its "Filled" row links the torrent as "Yes"."""
    return f'<html><body><a href="torrents.php?torrentid={torrent_id}">Yes</a></body></html>'


def _found(torrent_id: int, group_id: int) -> web.Response:
    return web.json_response(
        {"status": "success", "response": {"group": {"id": group_id}, "torrent": {"id": torrent_id}}}
    )


def _not_found() -> web.Response:
    return web.json_response({"status": "failure", "error": "bad parameters"})


class FakeTracker:
    """Answers upload.php as `upload` says and requests.php as `fill` says; records each request it gets."""

    def __init__(self, upload: str = "fill", fill: str = "filled", group_lookup: str = "redirect") -> None:
        self.upload = upload
        self.fill = fill
        self.group_lookup = group_lookup
        self.hits: list[tuple[str, str]] = []
        # Answers to ajax.php?action=torrent, the lookup by infohash, in order.
        self.lookups: list[web.Response] = []
        self.url = ""

    async def handle(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        self.hits.append((request.method, request.path_qs))
        if request.path == "/upload.php":
            if self.upload == "fill":
                # Gazelle has stored the torrent: on to the fill of the request the form named.
                raise web.HTTPFound(
                    f"/requests.php?action=takefill&requestid={REQUEST_ID}&torrentid={TORRENT_ID}&auth={AUTHKEY}"
                )
            if self.upload == "fill-without-torrentid":
                raise web.HTTPFound(f"/requests.php?action=view&id={REQUEST_ID}")
            # A page of its own: WhatCD's for a torrent that is not private, after storing it.
            page = (
                "<html><h1>Warning</h1><p>Your torrent has been uploaded; however, you must download your torrent "
                f'from <a href="torrents.php?id={GROUP_ID}">here</a>.</p></html>'
            )
            return web.Response(text=page, content_type="text/html")
        if request.path == "/requests.php" and request.query["action"] == "takefill":
            if self.fill == "filled":
                raise web.HTTPFound(f"/requests.php?action=view&id={REQUEST_ID}")
            return web.Response(text=_error_page(self.fill), content_type="text/html")
        if request.path == "/requests.php":
            if self.upload == "fill-without-torrentid":
                return web.Response(text="<html><body>A request page salmon cannot read</body></html>")
            return web.Response(text=_filled_request_page(TORRENT_ID), content_type="text/html")
        if request.path == "/torrents.php" and "id" in request.query:
            return web.Response(text="<html>the group page</html>", content_type="text/html")
        if request.path == "/torrents.php":
            if self.group_lookup == "redirect":
                raise web.HTTPFound(f"/torrents.php?id={GROUP_ID}&torrentid={request.query['torrentid']}")
            if self.group_lookup == "no-redirect":
                return web.Response(text="<html>no such torrent</html>", content_type="text/html")
            return web.Response(status=404, text="<html>404</html>", content_type="text/html")
        if request.path == "/ajax.php" and request.query.get("action") == "torrent":
            return self.lookups.pop(0)
        return web.Response(status=404)

    async def serve(self) -> tuple[web.AppRunner, str]:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        return runner, self.url


async def _upload(tracker: FakeTracker, files: UploadFiles, data: dict) -> tuple[int, int]:
    runner, url = await tracker.serve()
    api = FakeApi(url)
    try:
        return await api.upload(data, files)
    finally:
        await api.close()
        await runner.cleanup()


TAKEFILL = f"/requests.php?action=takefill&requestid={REQUEST_ID}&torrentid={TORRENT_ID}&auth={AUTHKEY}"


# A request fill


def test_a_fill_the_tracker_refuses_keeps_the_upload_and_names_the_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tracker = FakeTracker(fill="This request has already been filled.")
    files, _ = _upload_files(tmp_path)

    ids = anyio.run(_upload, tracker, files, {"type": 0})

    assert ids == (TORRENT_ID, GROUP_ID)
    # The upload, the fill it redirects to, then the group of the torrent: the hop to it, and its page.
    assert tracker.hits == [
        ("POST", "/upload.php"),
        ("GET", TAKEFILL),
        ("GET", f"/torrents.php?torrentid={TORRENT_ID}"),
        ("GET", f"/torrents.php?id={GROUP_ID}&torrentid={TORRENT_ID}"),
    ]
    printed = capsys.readouterr()
    # The request's own page, not the fill's URL, which carries the authkey.
    request_page = f"{tracker.url}/requests.php?action=view&id={REQUEST_ID}"
    assert f"The upload went through, but filling {request_page} failed: This request has already been filled." in (
        printed.out
    )
    assert AUTHKEY not in printed.out + printed.err


@pytest.mark.parametrize("fill", ["filled", "This request has already been filled."], ids=["filled", "refused"])
def test_a_fill_into_a_known_group_does_not_look_the_group_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fill: str
) -> None:
    tracker = FakeTracker(fill=fill)
    files, _ = _upload_files(tmp_path)

    ids = anyio.run(_upload, tracker, files, {"type": 0, "groupid": GROUP_ID})

    assert ids == (TORRENT_ID, GROUP_ID)
    assert ("GET", f"/torrents.php?torrentid={TORRENT_ID}") not in tracker.hits
    # The upload and the fill's hops only: two when the fill is refused, three when it goes through.
    assert len(tracker.hits) == (3 if fill == "filled" else 2)
    printed = capsys.readouterr()
    assert AUTHKEY not in printed.out + printed.err


@pytest.mark.parametrize("group_lookup", ["fails", "no-redirect"])
def test_a_filled_request_whose_group_lookup_fails_is_still_an_upload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], group_lookup: str
) -> None:
    tracker = FakeTracker(fill="filled", group_lookup=group_lookup)
    files, _ = _upload_files(tmp_path)

    ids = anyio.run(_upload, tracker, files, {"type": 0})

    # No group: the upload stands, and the caller offers no conversions into group 0.
    assert ids == (TORRENT_ID, 0)
    # The upload, the fill's two hops, and one group lookup: a 404 is not asked again.
    assert tracker.hits == [
        ("POST", "/upload.php"),
        ("GET", TAKEFILL),
        ("GET", f"/requests.php?action=view&id={REQUEST_ID}"),
        ("GET", f"/torrents.php?torrentid={TORRENT_ID}"),
    ]
    out = capsys.readouterr().out
    assert "Filled request: " in out
    if group_lookup == "fails":
        assert f"Could not look up the group of torrent {TORRENT_ID}: " in out
    else:
        assert "no Redirect found" in out


def test_a_fill_page_that_names_no_torrent_is_looked_up_by_infohash(tmp_path: Path) -> None:
    tracker = FakeTracker(upload="fill-without-torrentid")
    files, infohash = _upload_files(tmp_path)
    tracker.lookups = [_found(TORRENT_ID, GROUP_ID)]

    ids = anyio.run(_upload, tracker, files, {"type": 0})

    assert ids == (TORRENT_ID, GROUP_ID)
    assert tracker.hits == [
        ("POST", "/upload.php"),
        ("GET", f"/requests.php?action=view&id={REQUEST_ID}"),
        ("GET", f"/ajax.php?action=torrent&hash={infohash}"),
    ]


# An answer salmon cannot read


def test_an_upload_page_that_is_not_the_group_page_is_looked_up_by_infohash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tracker = FakeTracker(upload="warning")
    files, infohash = _upload_files(tmp_path)
    tracker.lookups = [_found(TORRENT_ID, GROUP_ID)]

    ids = anyio.run(_upload, tracker, files, {"type": 0})

    assert ids == (TORRENT_ID, GROUP_ID)
    assert tracker.hits == [("POST", "/upload.php"), ("GET", f"/ajax.php?action=torrent&hash={infohash}")]
    assert "its answer was not the group page: <html><h1>Warning</h1>" in capsys.readouterr().out


def test_the_upload_forms_own_error_is_a_failed_upload_with_no_lookup(tmp_path: Path) -> None:
    files, _ = _upload_files(tmp_path)
    hits: list[str] = []
    long_error = "The torrent is a dupe. " * 100

    async def upload_php(request: web.Request) -> web.Response:
        await request.read()
        hits.append(request.method)
        # The upload form again, with the announce URL it shows and the error above it.
        page = (
            f'<html><p style="color: red; text-align: center;">{long_error}</p>'
            f'<input value="{FakeApi.tracker_url}/{PASSKEY}/announce"></html>'
        )
        return web.Response(text=page, content_type="text/html")

    async def run() -> None:
        app = web.Application()
        app.router.add_route("*", "/upload.php", upload_php)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}")
        try:
            with pytest.raises(RequestError, match="Site upload failed: The torrent is a dupe.") as caught:
                await api.upload({"type": 0}, files)
            assert not isinstance(caught.value, UnknownOutcomeError)
            # Cut short: the error is a whole paragraph.
            assert len(str(caught.value)) < 600
        finally:
            await api.close()
            await runner.cleanup()

    anyio.run(run)
    assert hits == ["POST"]


@pytest.mark.parametrize(
    "answer",
    [
        "<html><body>502 Bad Gateway, as a 200</body></html>",
        # OPS's Gazelle prints a failed fill's error ahead of the upload's JSON (requests/fill_handle.php).
        'You do not have permission to fill this request.{"status":"success","response":{"torrentId":42}}',
    ],
    ids=["html", "text-then-json"],
)
def test_an_api_upload_answer_that_is_not_json_is_looked_up_by_infohash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], answer: str
) -> None:
    files, infohash = _upload_files(tmp_path)
    hits: list[tuple[str, str]] = []
    lookups = [_not_found(), _found(TORRENT_ID, GROUP_ID)]

    async def ajax(request: web.Request) -> web.Response:
        await request.read()
        hits.append((request.method, request.query["action"]))
        if request.method == "POST":
            return web.Response(text=answer, content_type="text/html")
        assert request.query["hash"] == infohash
        return lookups.pop(0)

    async def run() -> tuple[int, int]:
        app = web.Application()
        app.router.add_route("*", "/ajax.php", ajax)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}", api_key="an-api-key")
        try:
            return await api.upload({"type": 0}, files)
        finally:
            await api.close()
            await runner.cleanup()

    assert anyio.run(run) == (TORRENT_ID, GROUP_ID)
    # The upload once, then the lookup: not found yet, so once more, and no more.
    assert hits == [("POST", "upload"), ("GET", "torrent"), ("GET", "torrent")]
    assert "its answer was not JSON (200): " in capsys.readouterr().out


@pytest.mark.parametrize("answer", [{"status": "success"}, {"status": "success", "response": None}], ids=["no", "null"])
def test_an_api_success_without_its_response_is_looked_up_by_infohash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], answer: dict
) -> None:
    files, infohash = _upload_files(tmp_path)
    hits: list[str] = []

    async def ajax(request: web.Request) -> web.Response:
        await request.read()
        hits.append(request.method)
        if request.method == "POST":
            return web.json_response(answer)
        assert request.query["hash"] == infohash
        return _found(TORRENT_ID, GROUP_ID)

    async def run() -> tuple[int, int]:
        app = web.Application()
        app.router.add_route("*", "/ajax.php", ajax)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}", api_key="an-api-key")
        try:
            return await api.upload({"type": 0}, files)
        finally:
            await api.close()
            await runner.cleanup()

    assert anyio.run(run) == (TORRENT_ID, GROUP_ID)
    assert hits == ["POST", "GET"]
    assert "its answer named no torrent" in capsys.readouterr().out


# The group of an API upload


@pytest.mark.parametrize(
    ("data", "group_id"), [({"type": 0, "groupid": GROUP_ID}, GROUP_ID), ({"type": 0}, 0)], ids=["known", "new"]
)
def test_an_api_answer_without_the_group_keeps_the_group_it_was_sent_into(
    tmp_path: Path, data: dict, group_id: int
) -> None:
    files, _ = _upload_files(tmp_path)
    hits: list[str] = []

    async def ajax(request: web.Request) -> web.Response:
        await request.read()
        hits.append(request.method)
        return web.json_response({"status": "success", "response": {"torrentid": TORRENT_ID}})

    async def run() -> tuple[int, int]:
        app = web.Application()
        app.router.add_route("*", "/ajax.php", ajax)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}", api_key="an-api-key")
        try:
            return await api.upload(dict(data), files)
        finally:
            await api.close()
            await runner.cleanup()

    # 0 for a new group: the caller then offers no conversions, which would each start one.
    assert anyio.run(run) == (TORRENT_ID, group_id)
    assert hits == ["POST"]


# A rate limit page that leaves the upload's outcome unknown


def test_the_lookup_after_an_upload_answered_with_a_rate_limit_page_waits_for_it(tmp_path: Path) -> None:
    files, infohash = _upload_files(tmp_path)
    arrivals: list[tuple[str, float]] = []

    async def ajax(request: web.Request) -> web.Response:
        await request.read()
        arrivals.append((request.method, time.monotonic()))
        if request.method == "POST":
            # Not a 429: the tracker may have taken the upload before answering.
            return web.Response(status=503, text="Rate limit exceeded", headers={"Retry-After": "2"})
        assert request.query["hash"] == infohash
        return _found(TORRENT_ID, GROUP_ID)

    async def run() -> tuple[int, int]:
        app = web.Application()
        app.router.add_route("*", "/ajax.php", ajax)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}", api_key="an-api-key")
        # The tracker account's own rate limit, as salmon sends with.
        api._rate_limiter = None
        try:
            return await api.upload({"type": 0}, files)
        finally:
            await api.close()
            await runner.cleanup()

    assert anyio.run(run) == (TORRENT_ID, GROUP_ID)
    # Sent once, then looked up once, after the wait the tracker asked for.
    assert [method for method, _ in arrivals] == ["POST", "GET"]
    assert arrivals[1][1] - arrivals[0][1] >= 1.9


async def _api_upload_answered(tmp_path: Path, answer: dict, lookups: list[web.Response]) -> tuple[list, tuple]:
    files, _ = _upload_files(tmp_path)
    hits: list[tuple[str, str]] = []

    async def ajax(request: web.Request) -> web.Response:
        await request.read()
        hits.append((request.method, request.query["action"]))
        return web.json_response(answer) if request.method == "POST" else lookups.pop(0)

    app = web.Application()
    app.router.add_route("*", "/ajax.php", ajax)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}", api_key="an-api-key")
    try:
        return hits, await api.upload({"type": 0}, files)
    finally:
        await api.close()
        await runner.cleanup()


def test_an_api_upload_success_naming_no_torrent_is_looked_up_by_infohash(tmp_path: Path) -> None:
    answer = {"status": "success", "response": {"groupid": GROUP_ID}}

    hits, ids = anyio.run(_api_upload_answered, tmp_path, answer, [_found(TORRENT_ID, GROUP_ID)])

    assert ids == (TORRENT_ID, GROUP_ID)
    assert hits == [("POST", "upload"), ("GET", "torrent")]


def test_an_api_upload_refused_without_a_reason_is_a_failed_upload(tmp_path: Path) -> None:
    with pytest.raises(RequestError, match="API upload failed"):
        anyio.run(_api_upload_answered, tmp_path, {"status": "failure"}, [])


@pytest.mark.parametrize(("value", "expected"), [("42", 42), ("0", None), ("", None), ("4\u00b2", None)])
def test_an_id_in_a_query_is_a_number_above_0(value: str, expected: int | None) -> None:
    assert _query_id({"torrentid": [value]}, "torrentid") == expected
