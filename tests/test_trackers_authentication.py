"""A client authenticates once, however many of its requests are waiting for it (#468).

A fresh client's first request fetches the authkey with ajax.php?action=index. Requests that went out together
each used to send their own index. They now share one attempt: when it fails, it fails for all of them with one
error, instead of each of them trying again. Tests talk only to a local fake tracker.
"""

import asyncio
import gc
from collections import Counter
from collections.abc import Awaitable, Callable

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from tenacity import wait_none

from salmon.errors import LoginError
from salmon.trackers.base import BaseGazelleApi, RetryableError


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        super().__init__()
        # Per instance, as each test runs on its own event loop. The client is left fresh, not authenticated.
        self._rate_limiter = AsyncLimiter(100, 1)


class FakeTracker:
    """A local tracker logging each request it gets, whose index answers as the test sets it up."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        # The statuses the next index requests get, in turn, then index_status.
        self.index_answers: list[int] = []
        self.index_status = 200
        # While hold_index is set, an index is answered only once release_index is.
        self.hold_index = False
        self.release_index = asyncio.Event()
        self.index_arrived = asyncio.Event()
        # Index requests the client hung up on before their answer.
        self.cut_off = 0

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/ajax.php", self.ajax)
        # A held answer stops when the client hangs up, as a real server's would.
        self.runner = web.AppRunner(app, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def ajax(self, request: web.Request) -> web.Response:
        action = request.query["action"]
        if action != "index":
            self.requests.append(f"{action} {request.query['searchstr']}")
            return web.json_response({"status": "success", "response": {"results": []}})
        self.requests.append("index")
        self.index_arrived.set()
        if self.hold_index:
            try:
                await self.release_index.wait()
            except asyncio.CancelledError:
                self.cut_off += 1
                raise
        status = self.index_answers.pop(0) if self.index_answers else self.index_status
        if status == 401:
            return web.json_response({"status": "failure", "error": "bad key"}, status=401)
        if status != 200:
            return web.Response(status=status, text="down")
        return web.json_response({"status": "success", "response": {"authkey": "a", "passkey": "p"}})


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting, so the tests count attempts, not seconds."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


def _run(body: Callable[[FakeTracker, FakeApi], Awaitable[None]]) -> None:
    """Run a test against a fresh fake tracker and client, then fail on anything left unclosed."""
    unclosed: list[str] = []

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        tracker = FakeTracker()
        api = FakeApi(await tracker.start())
        try:
            with anyio.fail_after(10):
                await body(tracker, api)
        finally:
            await api.close()
            tracker.release_index.set()
            await tracker.runner.cleanup()
        del tracker, api
        # An unclosed session or connector, or an unretrieved task error, is only reported once it is collected.
        gc.collect()
        await anyio.sleep(0.05)

    anyio.run(main)
    assert unclosed == []


async def _browse(api: FakeApi, i: int) -> dict:
    return await api.api_call("browse", {"searchstr": str(i)})


def _browses(*ids: int) -> Counter[str]:
    return Counter(f"browse {i}" for i in ids)


@pytest.mark.parametrize("n", [1, 2, 5])
def test_requests_gathered_on_a_fresh_client_send_one_index(n: int) -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        await asyncio.gather(*(_browse(api, i) for i in range(n)))
        assert tracker.requests[0] == "index"
        assert Counter(tracker.requests[1:]) == _browses(*range(n))

    _run(body)


def test_an_index_that_fails_once_is_retried_by_the_one_attempt() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        tracker.index_answers = [500]
        await asyncio.gather(*(_browse(api, i) for i in range(5)))
        # The failed index and its retry, then the requests that waited for them.
        assert tracker.requests[:2] == ["index", "index"]
        assert Counter(tracker.requests[2:]) == _browses(*range(5))

    _run(body)


def test_an_index_that_keeps_failing_fails_every_waiting_request_with_one_error() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        tracker.index_status = 500
        results = await asyncio.gather(*(_browse(api, i) for i in range(5)), return_exceptions=True)
        assert isinstance(results[0], RetryableError)
        assert all(result is results[0] for result in results)
        # The five tries of the one attempt. The requests waiting on it do not try again for themselves.
        assert tracker.requests == ["index"] * 5

    _run(body)


def test_a_rejected_key_fails_every_waiting_request_with_one_error(capsys: pytest.CaptureFixture[str]) -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        tracker.index_status = 401
        results = await asyncio.gather(*(_browse(api, i) for i in range(5)), return_exceptions=True)
        assert isinstance(results[0], LoginError)
        assert all(result is results[0] for result in results)
        assert tracker.requests == ["index"]

    _run(body)
    assert capsys.readouterr().out.count("Authentication to RED failed") == 1


def test_a_request_after_a_failed_attempt_tries_again() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        tracker.index_status = 401
        with pytest.raises(LoginError):
            await _browse(api, 0)
        tracker.index_status = 200
        await _browse(api, 1)
        assert tracker.requests == ["index", "index", "browse 1"]

    _run(body)


def test_cancelling_one_waiting_request_leaves_the_attempt_to_the_others() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        tracker.hold_index = True
        tasks = [asyncio.create_task(_browse(api, i)) for i in range(3)]
        await tracker.index_arrived.wait()
        tasks[0].cancel()
        # The cancelled request is gone before the index is answered.
        await anyio.sleep(0.05)
        tracker.release_index.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1:] == [{"results": []}] * 2
        assert tracker.requests[0] == "index"
        assert Counter(tracker.requests[1:]) == _browses(1, 2)
        assert tracker.cut_off == 0
        # The client is authenticated for what comes next.
        await _browse(api, 3)
        assert Counter(tracker.requests[1:]) == _browses(1, 2, 3)

    _run(body)


def test_cancelling_every_waiting_request_cancels_the_attempt() -> None:
    async def body(tracker: FakeTracker, api: FakeApi) -> None:
        tracker.hold_index = True
        tasks = [asyncio.create_task(_browse(api, i)) for i in range(2)]
        await tracker.index_arrived.wait()
        for task in tasks:
            task.cancel()
        # A request made as they leave, before the attempt they leave is over.
        tracker.hold_index = False
        late = asyncio.create_task(_browse(api, 2))
        await asyncio.gather(*tasks, return_exceptions=True)
        # With nobody waiting for it, the index is cut off, not left running on its own.
        with anyio.fail_after(1):
            while not tracker.cut_off:
                await anyio.sleep(0.01)
        # The client is not stuck: the late request makes an attempt of its own and goes through.
        assert await late == {"results": []}
        assert tracker.requests == ["index", "index", "browse 2"]
        assert tracker.cut_off == 1

    _run(body)
