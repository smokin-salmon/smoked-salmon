import asyncio
import gc
import signal
import socket
import sys
from pathlib import Path

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from tenacity import wait_none
from torf import Torrent

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.common import UploadFiles
from salmon.errors import LoginError, RequestError, UnknownOutcomeError
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi, RetryableError
from salmon.uploader import spectrals


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = base_url
        self.api_key = api_key
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting, so the tests count attempts, not seconds."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


def _wait_before_lookups(monkeypatch: pytest.MonkeyPatch, first: float, second: float) -> None:
    """Set how long a lost upload is waited for before its first and its second lookup."""
    monkeypatch.setattr(base, "_LOST_UPLOAD_FIRST_WAIT", first)
    monkeypatch.setattr(base, "_LOST_UPLOAD_SECOND_WAIT", second)


@pytest.fixture(autouse=True)
def no_lost_upload_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Look a lost upload up without waiting, except in the tests that time the waits."""
    _wait_before_lookups(monkeypatch, 0, 0)


async def _serve(**handlers) -> tuple[web.AppRunner, str]:
    app = web.Application()
    for path, handler in handlers.items():
        app.router.add_route("*", f"/{path}.php", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


async def _drop(request: web.Request) -> web.Response:
    """Close the connection instead of answering, as when the answer is lost on the way back."""
    assert request.transport is not None
    request.transport.close()
    return web.Response()


def _found(torrent_id: int, group_id: int) -> web.Response:
    return web.json_response(
        {"status": "success", "response": {"group": {"id": group_id}, "torrent": {"id": torrent_id}}}
    )


def _not_found() -> web.Response:
    return web.json_response({"status": "failure", "error": "bad parameters"})


def _upload_files(tmp_path: Path) -> tuple[UploadFiles, str]:
    album = tmp_path / "album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"not really audio" * 64)
    torrent = Torrent(album, trackers=["http://tracker.invalid/announce"], private=True, source="RED")
    torrent.generate()
    return UploadFiles(torrent_data=torrent.dump()), torrent.infohash.upper()


async def _upload_with_a_lost_answer_is_sent_once() -> None:
    posts = []

    async def ajax(request: web.Request) -> web.Response:
        await request.read()
        posts.append(request.query["action"])  # the tracker has taken the upload here
        return await _drop(request)

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        with pytest.raises(UnknownOutcomeError):
            await api._request("POST", url + "/ajax.php?action=upload", data={"file": "x"}, prefer_api_key=True)
        assert posts == ["upload"]
    finally:
        await api.close()
        await runner.cleanup()


async def _lost_upload_found_by_infohash(tmp_path: Path) -> None:
    files, infohash = _upload_files(tmp_path)
    posts, lookups = [], []

    async def ajax(request: web.Request) -> web.Response:
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            return await _drop(request)
        lookups.append(request.query["hash"])
        return _found(9, 5)

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        assert await api.upload({"type": 0}, files) == (9, 5)
        assert posts == ["upload"]
        assert lookups == [infohash]
    finally:
        await api.close()
        await runner.cleanup()


async def _lost_upload_not_found_says_it_may_have_gone_through(tmp_path: Path) -> None:
    files, _ = _upload_files(tmp_path)
    posts, lookups = [], []

    async def ajax(request: web.Request) -> web.Response:
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            return await _drop(request)
        lookups.append(request.query["hash"])
        return _not_found()

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        with pytest.raises(
            UnknownOutcomeError,
            match=r"did not find it \(bad parameters\)\. The upload may still have gone through: check your uploads",
        ):
            await api.upload({"type": 0}, files)
        assert posts == ["upload"]
        # Looked up once more after the second wait, and no more.
        assert len(lookups) == 2
    finally:
        await api.close()
        await runner.cleanup()


# Each wait before a lookup, in the tests that time them. The torrent appears half a wait away from
# any lookup, so these tests hold as long as each lookup goes out less than half a wait late.
WAIT = 0.4


async def _upload_whose_torrent_appears_later(tmp_path: Path, appears_after: float) -> tuple[list[str], list[float]]:
    """Upload to a tracker that loses the answer, and has the torrent `appears_after` seconds after it.

    Returns the uploads the tracker got and when each lookup came, in seconds after the upload.
    """
    files, infohash = _upload_files(tmp_path)
    posts: list[str] = []
    lookups: list[float] = []
    uploaded_at = 0.0

    async def ajax(request: web.Request) -> web.Response:
        nonlocal uploaded_at
        now = asyncio.get_running_loop().time()
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            uploaded_at = now
            return await _drop(request)
        assert request.query["hash"] == infohash
        lookups.append(now - uploaded_at)
        return _found(9, 5) if now - uploaded_at >= appears_after else _not_found()

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        assert await api.upload({"type": 0}, files) == (9, 5)
        return posts, lookups
    finally:
        await api.close()
        await runner.cleanup()


async def _lost_upload_lookup_that_fails_otherwise_is_not_repeated(
    tmp_path: Path, status: int, raised: type[RequestError]
) -> None:
    files, _ = _upload_files(tmp_path)
    posts, lookups = [], []

    async def ajax(request: web.Request) -> web.Response:
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            return await _drop(request)
        lookups.append(request.query["hash"])
        return web.json_response({"status": "failure", "error": "no"}, status=status)

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        with pytest.raises(UnknownOutcomeError, match="may still have gone through") as caught:
            await api.upload({"type": 0}, files)
        assert isinstance(caught.value.__cause__, raised)
        assert posts == ["upload"]
        # Only a tracker saying it does not have the torrent is worth asking again later.
        assert len(lookups) == (5 if raised is RetryableError else 1)
    finally:
        await api.close()
        await runner.cleanup()


async def _upload_after_a_kept_alive_connection_goes_on_a_fresh_one(tmp_path: Path) -> None:
    files, _ = _upload_files(tmp_path)
    seen = set()
    posts = []

    async def ajax(request: web.Request) -> web.Response:
        reused = request.transport in seen
        seen.add(request.transport)
        if request.method == "GET":
            return web.json_response({"status": "success", "response": {"authkey": "a", "passkey": "p"}})
        posts.append("reused" if reused else "fresh")
        if reused:
            # The tracker drops a kept-alive connection as the next request comes in on
            # it, as when its idle timeout fires just as the request is sent. It never
            # read the upload.
            return await _drop(request)
        await request.read()
        return web.json_response({"status": "success", "response": {"torrentid": 9, "groupid": 5}})

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        await api.api_call("index")
        assert await api.upload({"type": 0}, files) == (9, 5)
        assert posts == ["fresh"]
    finally:
        await api.close()
        await runner.cleanup()


async def _rate_limited_upload_is_retried(tmp_path: Path) -> None:
    files, _ = _upload_files(tmp_path)
    posts = []

    async def ajax(request: web.Request) -> web.Response:
        await request.read()
        posts.append(request.query["action"])
        if len(posts) == 1:
            # A 429 means the tracker did nothing, so the upload can be sent again.
            return web.json_response(
                {"status": "failure", "error": "Rate limit exceeded"}, status=429, headers={"Retry-After": "0"}
            )
        return web.json_response({"status": "success", "response": {"torrentid": 9, "groupid": 5}})

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        assert await api.upload({"type": 0}, files) == (9, 5)
        assert posts == ["upload", "upload"]
    finally:
        await api.close()
        await runner.cleanup()


async def _server_error_after_the_upload_redirect_does_not_resend_it(tmp_path: Path) -> None:
    files, infohash = _upload_files(tmp_path)
    hits = []

    async def upload(request: web.Request) -> web.Response:
        await request.read()
        hits.append((request.method, request.path))
        raise web.HTTPFound("/torrents.php?id=5")

    async def torrents(request: web.Request) -> web.Response:
        hits.append((request.method, request.path))
        return web.Response(status=500, text="Internal Server Error")

    async def ajax(request: web.Request) -> web.Response:
        hits.append((request.method, request.path))
        assert request.query["hash"] == infohash
        return _found(9, 5)

    runner, url = await _serve(upload=upload, torrents=torrents, ajax=ajax)
    api = FakeApi(url)
    try:
        # The redirect is the tracker's answer: the upload went through, only the page after it failed.
        assert await api.upload({"type": 0}, files) == (9, 5)
        assert hits == [("POST", "/upload.php"), ("GET", "/torrents.php"), ("GET", "/ajax.php")]
    finally:
        await api.close()
        await runner.cleanup()


async def _report_with_a_lost_answer_is_sent_once() -> None:
    posts = []

    async def reportsv2(request: web.Request) -> web.Response:
        await request.read()
        posts.append(request.query["action"])
        return await _drop(request)

    runner, url = await _serve(reportsv2=reportsv2)
    api = FakeApi(url)
    try:
        with pytest.raises(UnknownOutcomeError):
            await api.report_lossy_master(9, "a comment", "WEB")
        assert posts == ["takereport"]
        # The upload flow says the report may have been filed, and goes on to seed the upload.
        await spectrals.report_lossy_master(api, 9, None, None, "WEB", "a comment")
        assert posts == ["takereport", "takereport"]
    finally:
        await api.close()
        await runner.cleanup()


async def _gets_are_still_retried() -> None:
    hits = []

    async def log(request: web.Request) -> web.Response:
        hits.append(request.path)
        if len(hits) < 3:
            return await _drop(request)
        return web.Response(text="log page")

    runner, url = await _serve(log=log)
    api = FakeApi(url)
    try:
        resp = await api._request("GET", url + "/log.php", params={"page": 1})
        assert resp.text == "log page"
        assert len(hits) == 3
    finally:
        await api.close()
        await runner.cleanup()


async def _exhausted_retries_raise_a_request_error() -> None:
    hits = []

    async def log(request: web.Request) -> web.Response:
        hits.append(request.path)
        return web.Response(status=503, text="Service Unavailable")

    runner, url = await _serve(log=log)
    api = FakeApi(url)
    try:
        # A RequestError is what the upload flow catches, so the run carries on to the next tracker.
        with pytest.raises(RequestError):
            await api._request("GET", url + "/log.php", params={"page": 1})
        assert len(hits) == 5
    finally:
        await api.close()
        await runner.cleanup()


async def _upload_that_cannot_connect_is_retried() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    api = FakeApi(f"http://127.0.0.1:{port}", api_key="an-api-key")
    try:
        # Nothing reached the tracker, so sending the upload again is safe.
        with pytest.raises(RetryableError):
            await api._request("POST", api.base_url + "/ajax.php?action=upload", data={"file": "x"})
        assert BaseGazelleApi._send.statistics["attempt_number"] == 5  # type: ignore[attr-defined]
    finally:
        await api.close()


def test_upload_with_a_lost_answer_is_sent_once() -> None:
    anyio.run(_upload_with_a_lost_answer_is_sent_once)


def test_lost_upload_found_by_infohash(tmp_path: Path) -> None:
    anyio.run(_lost_upload_found_by_infohash, tmp_path)


def test_lost_upload_not_found_says_it_may_have_gone_through(tmp_path: Path) -> None:
    anyio.run(_lost_upload_not_found_says_it_may_have_gone_through, tmp_path)


def test_lost_upload_that_appears_during_the_first_wait_is_found_on_the_first_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wait_before_lookups(monkeypatch, WAIT, WAIT)
    # Looked up at once, as it used to be, the tracker does not have it yet.
    posts, lookups = anyio.run(_upload_whose_torrent_appears_later, tmp_path, WAIT / 2)
    assert posts == ["upload"]
    assert len(lookups) == 1
    assert lookups[0] >= WAIT


def test_lost_upload_that_appears_during_the_second_wait_is_found_on_the_second_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wait_before_lookups(monkeypatch, WAIT, WAIT)
    posts, lookups = anyio.run(_upload_whose_torrent_appears_later, tmp_path, WAIT * 1.5)
    assert posts == ["upload"]
    assert len(lookups) == 2
    assert lookups[0] >= WAIT
    assert lookups[1] >= WAIT * 2


@pytest.mark.parametrize(("status", "raised"), [(503, RetryableError), (401, LoginError)])
def test_lost_upload_lookup_that_fails_otherwise_is_not_repeated(
    tmp_path: Path, status: int, raised: type[RequestError]
) -> None:
    anyio.run(_lost_upload_lookup_that_fails_otherwise_is_not_repeated, tmp_path, status, raised)


def test_ctrl_c_while_waiting_for_a_lost_upload_sends_nothing_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wait_before_lookups(monkeypatch, 30, 30)
    files, _ = _upload_files(tmp_path)
    posts, lookups, unclosed = [], [], []

    async def ajax(request: web.Request) -> web.Response:
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            # The user presses Ctrl-C a moment later, once salmon waits to look the upload up.
            asyncio.get_running_loop().call_later(0.5, signal.raise_signal, signal.SIGINT)
            return await _drop(request)
        lookups.append(request.query["hash"])
        return _found(9, 5)

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        runner, url = await _serve(ajax=ajax)
        api = FakeApi(url, api_key="an-api-key")
        try:
            with anyio.fail_after(5):
                await api.upload({"type": 0}, files)
        finally:
            await api.close()
            await runner.cleanup()
            # A little longer, for a lookup sent anyway to arrive.
            await anyio.sleep(0.2)
            del runner, api
            # An unclosed session or connector is only reported once it is collected.
            gc.collect()
            await anyio.sleep(0.05)

    # Ctrl-C as in a terminal, also when the tests were started in the background, where it is ignored.
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        # As in salmon itself: anyio.run turns Ctrl-C into cancelling the run, then raises KeyboardInterrupt.
        with pytest.raises(KeyboardInterrupt):
            anyio.run(main)
    finally:
        signal.signal(signal.SIGINT, previous)
    out = capsys.readouterr().out
    assert "Waiting 30 s for RED to finish processing it" in out
    assert "may still have gone through: check your uploads on RED" in out
    assert posts == ["upload"]
    assert lookups == []
    assert unclosed == []


def test_upload_after_a_kept_alive_connection_goes_on_a_fresh_one(tmp_path: Path) -> None:
    anyio.run(_upload_after_a_kept_alive_connection_goes_on_a_fresh_one, tmp_path)


def test_rate_limited_upload_is_retried(tmp_path: Path) -> None:
    anyio.run(_rate_limited_upload_is_retried, tmp_path)


def test_server_error_after_the_upload_redirect_does_not_resend_it(tmp_path: Path) -> None:
    anyio.run(_server_error_after_the_upload_redirect_does_not_resend_it, tmp_path)


def test_report_with_a_lost_answer_is_sent_once() -> None:
    anyio.run(_report_with_a_lost_answer_is_sent_once)


def test_gets_are_still_retried() -> None:
    anyio.run(_gets_are_still_retried)


def test_exhausted_retries_raise_a_request_error() -> None:
    anyio.run(_exhausted_retries_raise_a_request_error)


def test_upload_that_cannot_connect_is_retried() -> None:
    anyio.run(_upload_that_cannot_connect_is_retried)
