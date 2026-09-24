"""The search for an existing group runs in the background during the checks before the dupe prompt.

upload() runs against a local fake tracker through a real BaseGazelleApi, with the steps that do not
talk to the tracker stubbed out. The MQA check stub stands for the checks the search overlaps (MQA,
upconvert, log), where the user may be answering prompts, so nothing may print meanwhile, and the
search must be over or cancelled whichever way upload() ends. The dupe prompt comes after those
checks and before the spectral step, as on master.
"""

import asyncio
import gc
import time
from collections import Counter
from pathlib import Path
from typing import Any

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

import salmon.trackers
import salmon.uploader
from salmon.errors import AbortAndDeleteFolder, LoginError, RequestFailedError, UploadError
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import dupe_checker

GROUP = {
    "groupId": 5,
    "groupName": "Hits Vol. 2",
    "artist": "Artist",
    "groupYear": 2020,
    "releaseType": "Album",
    "tags": ["rock"],
    "torrents": [],
}
# Two search strings, so the search is two gathered browse requests.
SEARCHES = ["artist hits vol. 2", "artist hits volume 2"]
# What master sends for this release: one index per gathered first request, the search, the
# request search and the group shown after the upload (the upload itself is stubbed out).
REQUESTS_WHEN_A_GROUP_IS_FOUND = [
    "index",
    "index",
    *(f"browse {s}" for s in SEARCHES),
    *(f"requests {s}" for s in SEARCHES),
    "torrentgroup 5",
]
LOG_PAGES = [f"log {page}" for page in range(1, 10)]


class FakeTracker:
    def __init__(self, browse_results: list | None = None, browse_status: int = 200, browse_delay: float = 0) -> None:
        self.browse_results = browse_results if browse_results is not None else [GROUP]
        self.browse_status = browse_status
        self.browse_delay = browse_delay
        self.hits: list[str] = []
        self.peers: dict[str, set[int]] = {}
        self.browse_arrived = anyio.Event()

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/ajax.php", self.ajax)
        app.router.add_get("/log.php", self.log)
        app.router.add_get("/login.php", self.login)
        # A slow answer stops when the client hangs up, as a real server's would.
        self.runner = web.AppRunner(app, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    def _hit(self, request: web.Request, what: str) -> None:
        self.hits.append(what)
        assert request.transport is not None
        instance = request.headers.get("X-Instance", "")
        self.peers.setdefault(instance, set()).add(request.transport.get_extra_info("peername")[1])

    async def ajax(self, request: web.Request) -> web.Response:
        action = request.query["action"]
        response: dict[str, Any] = {}
        if action == "index":
            self._hit(request, "index")
            response = {"authkey": "a", "passkey": "p"}
        elif action == "browse":
            self._hit(request, f"browse {request.query['searchstr']}")
            self.browse_arrived.set()
            await asyncio.sleep(self.browse_delay)
            if self.browse_status != 200:
                return web.json_response({"status": "failure", "error": "bad search"}, status=self.browse_status)
            response = {"results": self.browse_results}
        elif action == "requests":
            self._hit(request, f"requests {request.query['search']}")
            response = {"results": []}
        elif action == "torrentgroup":
            self._hit(request, f"torrentgroup {request.query['id']}")
            response = {
                "group": {"id": 5, "name": "Hits Vol. 2", "year": 2020, "musicInfo": {"artists": [{"name": "A"}]}},
                "torrents": [],
            }
        return web.json_response({"status": "success", "response": response})

    async def log(self, request: web.Request) -> web.Response:
        self._hit(request, f"log {request.query['page']}")
        if request.cookies.get("session") != "good":
            raise web.HTTPFound("/login.php")
        return web.Response(text="<html></html>", content_type="text/html")

    async def login(self, request: web.Request) -> web.Response:
        self._hit(request, "login")
        raise web.HTTPFound("/log.php")


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    tracker_url = "http://127.0.0.1"

    def __init__(self, base_url: str, cookie: str, api_key: str = "", instance: str = "upload") -> None:
        self.base_url = base_url
        self.cookie = cookie
        self.api_key = api_key
        super().__init__()
        self.headers["X-Instance"] = instance


class CountingLimiter(AsyncLimiter):
    def __init__(self, max_rate: float, time_period: float) -> None:
        super().__init__(max_rate, time_period)
        self.acquired = 0

    async def acquire(self, amount: float = 1) -> None:
        await super().acquire(amount)
        self.acquired += 1


class Flow:
    """upload() with every step that does not talk to the tracker stubbed out, and a log of what happened."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tracker: FakeTracker) -> None:
        self.monkeypatch = monkeypatch
        self.tracker = tracker
        self.events: list[str] = []
        # How the background search ended: "finished", "failed" or "cancelled".
        self.search = "not started"
        self.search_task: asyncio.Task | None = None
        # The checks the search overlaps: by default they take long enough for the search to be over.
        self.checks = self.wait_for_the_search_to_end
        self.api: FakeApi | None = None
        self.returned_at = 0.0

    def echo(self, event: str) -> None:
        self.events.append(event)
        salmon.uploader.click.echo(f"<<{event}>>")

    async def wait_for_the_search_to_arrive(self) -> None:
        with anyio.fail_after(5):
            await self.tracker.browse_arrived.wait()

    async def wait_for_the_search_to_end(self) -> None:
        with anyio.fail_after(5):
            while self.search in ("not started", "running"):
                await anyio.sleep(0.01)

    def install(self, path: str) -> None:
        flow = self
        search = dupe_checker.fetch_existing_group_candidates

        async def watched_search(*args, **kwargs):
            flow.search, flow.search_task = "running", asyncio.current_task()
            try:
                result = await search(*args, **kwargs)
            except anyio.get_cancelled_exc_class():
                flow.search = "cancelled"
                raise
            except Exception:
                flow.search = "failed"
                raise
            flow.search = "finished"
            return result

        def returning(result: Any = None):
            def fake(*_args, **_kwargs) -> Any:
                return result

            return fake

        def returning_async(result: Any = None):
            async def fake(*_args, **_kwargs) -> Any:
                return result

            return fake

        async def checks(*_args, **_kwargs) -> None:
            flow.echo("checks start")
            await flow.checks()
            flow.echo("checks end")

        async def check_spectrals(*_args, **_kwargs):
            flow.echo("spectral step")
            return False, None

        async def upload_and_report(*_args, **_kwargs):
            flow.echo("upload")
            return 1, 5, None, None, "url"

        async def prompt(text: str, *_args, **_kwargs) -> str:
            flow.echo("group prompt" if "existing group" in text else f"prompt: {text}")
            return "n"

        class FakeUploadManager:
            async def execute_upload(self) -> None:
                pass

        rls_data = {"format": "FLAC", "encoding": "Lossless", "artists": [("Artist", "main")], "title": "Hits Vol. 2"}
        rls_data["catno"] = None
        metadata = {**rls_data, "cover": None}
        for name, fake in {
            "gather_audio_info": returning({}),
            "check_hybrid": returning(False),
            "standardize_tags": returning(),
            "gather_tags": returning({}),
            "construct_rls_data": returning(rls_data),
            "mqa_test": checks,
            "check_spectrals": check_spectrals,
            "get_metadata": returning_async((metadata, None)),
            "edit_metadata": returning_async((path, metadata, {}, {})),
            "concat_track_data": returning({}),
            "get_spectrals_path": returning("/spectrals"),
            "handle_spectrals_upload_and_deletion": returning_async(),
            "resolve_cover_url": returning_async((True, None)),
            "UploadManager": FakeUploadManager,
            "upload_and_report": upload_and_report,
        }.items():
            self.monkeypatch.setattr(salmon.uploader, name, fake)
        self.monkeypatch.setattr(dupe_checker, "fetch_existing_group_candidates", watched_search)
        self.monkeypatch.setattr(salmon.uploader, "fetch_existing_group_candidates", watched_search, raising=False)
        self.monkeypatch.setattr(dupe_checker.click, "prompt", prompt)
        self.monkeypatch.setattr(salmon.uploader.click, "confirm", returning(False))
        self.monkeypatch.setattr(salmon.trackers, "choose_tracker", returning_async(None))
        self.monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", False)

    async def run(self, cookie: str = "good", api_key: str = "", path: str = "/release") -> None:
        self.install(path)
        self.api = FakeApi(await self.tracker.start(), cookie, api_key)
        try:
            await salmon.uploader.upload(self.api, path, None, "WEB", None, (), None)
        finally:
            self.returned_at = time.monotonic()
            await self.api.close()
            await self.tracker.runner.cleanup()


@pytest.fixture(autouse=True)
def _fast_limiter(monkeypatch: pytest.MonkeyPatch) -> CountingLimiter:
    # The shared limiter, at 5 per 0.5 s instead of 5 per 10 s. Class-wide, as in the real code.
    limiter = CountingLimiter(5, 0.5)
    monkeypatch.setattr(BaseGazelleApi, "_rate_limiter", limiter)
    return limiter


def _during_the_checks(out: str) -> str:
    return out.split("<<checks start>>\n", 1)[1].split("<<checks end>>", 1)[0]


def _before_the_spectral_step(out: str) -> str:
    return out.split("<<checks end>>", 1)[1].split("<<spectral step>>", 1)[0]


def test_the_search_runs_during_the_checks_and_sends_what_master_sends(monkeypatch, capsys) -> None:
    tracker = FakeTracker()
    flow = Flow(monkeypatch, tracker)

    anyio.run(flow.run)

    # The checks only ended once the search was over: the two overlapped. The dupe prompt then comes
    # before the spectral step, as on master.
    assert flow.events == ["checks start", "checks end", "group prompt", "spectral step", "upload"]
    assert Counter(tracker.hits) == Counter(REQUESTS_WHEN_A_GROUP_IS_FOUND)
    out = capsys.readouterr().out
    assert _during_the_checks(out) == ""
    assert "Results matching this release were found on RED" in _before_the_spectral_step(out)


def test_the_site_log_is_read_as_before_when_the_search_finds_nothing(monkeypatch, capsys) -> None:
    tracker = FakeTracker(browse_results=[])
    flow = Flow(monkeypatch, tracker)

    anyio.run(flow.run)

    assert Counter(tracker.hits) == Counter(REQUESTS_WHEN_A_GROUP_IS_FOUND + LOG_PAGES)
    assert flow.events == ["checks start", "checks end", "group prompt", "spectral step", "upload"]
    assert _during_the_checks(capsys.readouterr().out) == ""


def test_the_skipped_site_log_is_reported_before_the_spectral_step(monkeypatch, capsys) -> None:
    tracker = FakeTracker(browse_results=[])
    flow = Flow(monkeypatch, tracker)

    anyio.run(lambda: flow.run(cookie="", api_key="an-api-key"))

    assert Counter(tracker.hits) == Counter(REQUESTS_WHEN_A_GROUP_IS_FOUND)
    out = capsys.readouterr().out
    assert _during_the_checks(out) == ""
    assert "needs a session cookie" in _before_the_spectral_step(out)


def test_requests_sent_meanwhile_share_the_rate_limiter_and_the_pool(monkeypatch, _fast_limiter) -> None:
    tracker = FakeTracker()
    flow = Flow(monkeypatch, tracker)

    async def tracker_requests_meanwhile() -> None:
        # Anything else that talks to the tracker during the checks: on the upload's own
        # client, and on another one, as the code builds a client per tracker where it needs one.
        assert flow.api is not None
        other = FakeApi(flow.api.base_url, "good", instance="other")
        other._authenticated = True
        try:
            await asyncio.gather(
                *(flow.api.api_call("torrentgroup", {"id": 5}) for _ in range(3)),
                *(other.api_call("torrentgroup", {"id": 5}) for _ in range(3)),
            )
        finally:
            await other.close()
        await flow.wait_for_the_search_to_end()

    flow.checks = tracker_requests_meanwhile
    started = time.monotonic()

    anyio.run(flow.run)

    sent = len(tracker.hits)
    assert Counter(tracker.hits)["browse artist hits vol. 2"] == 1
    assert sent >= len(REQUESTS_WHEN_A_GROUP_IS_FOUND) + 6
    # Every request went through the one class-wide limiter, the background search's too...
    assert _fast_limiter.acquired == sent
    # ...so the run took at least as long as that limiter allows: a burst of 5, then 10 a second.
    assert flow.returned_at - started >= (sent - 5) / 10 - 0.05
    # The upload's own client, background search included, never opened more than its two connections.
    assert len(tracker.peers["upload"]) <= 2


def test_a_failed_search_is_reported_before_the_spectral_step_as_before(monkeypatch, capsys) -> None:
    tracker = FakeTracker(browse_status=400)
    flow = Flow(monkeypatch, tracker)

    with pytest.raises(RequestFailedError, match="bad search"):
        anyio.run(flow.run)

    assert flow.events == ["checks start", "checks end"]
    out = capsys.readouterr().out
    assert _during_the_checks(out) == ""
    assert "Request to RED failed (400)" in _before_the_spectral_step(out)


def test_an_expired_cookie_is_reported_before_the_spectral_step_as_before(monkeypatch, capsys) -> None:
    tracker = FakeTracker(browse_results=[])
    flow = Flow(monkeypatch, tracker)

    with pytest.raises(LoginError):
        anyio.run(lambda: flow.run(cookie="expired"))

    assert tracker.hits.count("log 1") == 1
    assert "login" not in tracker.hits
    out = capsys.readouterr().out
    assert flow.events == ["checks start", "checks end"]
    assert _during_the_checks(out) == ""
    assert "sent this request to its login page" in _before_the_spectral_step(out)


async def _leaves_nothing_behind(flow: Flow, run) -> None:
    unhandled: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        await run()
    finally:
        # An unretrieved task error is only reported once the task is collected.
        gc.collect()
        await anyio.sleep(0.05)
        assert flow.search_task is not None and flow.search_task.done()
        assert not unhandled


@pytest.mark.parametrize("exc", [salmon.uploader.click.Abort(), UploadError("Spectral IDs out of range.")])
def test_leaving_during_the_checks_cancels_the_search(monkeypatch, capsys, exc: BaseException) -> None:
    tracker = FakeTracker(browse_delay=30)
    flow = Flow(monkeypatch, tracker)

    async def leave() -> None:
        await flow.wait_for_the_search_to_arrive()
        raise exc

    flow.checks = leave
    started = time.monotonic()

    if isinstance(exc, UploadError):
        # Raised as is, not wrapped in an exception group.
        with pytest.raises(UploadError) as raised:
            anyio.run(_leaves_nothing_behind, flow, flow.run)
        assert raised.value is exc
    else:
        anyio.run(_leaves_nothing_behind, flow, flow.run)
        assert "Aborting upload..." in capsys.readouterr().out

    # upload() did not wait for the slow search: it cancelled it.
    assert flow.returned_at - started < 5
    assert flow.search == "cancelled"
    assert flow.events == ["checks start"]


def test_deleting_the_folder_during_the_checks_cancels_the_search(monkeypatch, tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    tracker = FakeTracker(browse_delay=30)
    flow = Flow(monkeypatch, tracker)
    monkeypatch.setattr(salmon.uploader.platform, "system", lambda: "Linux")

    async def delete() -> None:
        await flow.wait_for_the_search_to_arrive()
        raise AbortAndDeleteFolder

    flow.checks = delete

    anyio.run(_leaves_nothing_behind, flow, lambda: flow.run(path=str(release)))

    assert not release.exists()
    assert flow.search == "cancelled"


def test_aborting_after_the_search_failed_leaves_no_unretrieved_error(monkeypatch, capsys) -> None:
    tracker = FakeTracker(browse_status=400)
    flow = Flow(monkeypatch, tracker)

    async def abort_once_it_failed() -> None:
        await flow.wait_for_the_search_to_end()
        raise salmon.uploader.click.Abort

    flow.checks = abort_once_it_failed

    anyio.run(_leaves_nothing_behind, flow, flow.run)

    assert flow.search == "failed"
    out = capsys.readouterr().out
    assert "Aborting upload..." in out
    # The user left: the failure of a search they no longer need is not reported.
    assert "Request to RED failed" not in out
