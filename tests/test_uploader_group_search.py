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

import anyio
import asyncclick as click
import pytest
from aiolimiter import AsyncLimiter
from helpers.uploader_group_search import FakeApi, FakeTracker, Flow

import salmon.trackers
import salmon.uploader
from salmon.errors import AbortAndDeleteFolder, UploadError
from salmon.trackers.base import BaseGazelleApi

# Two search strings, so the search is two gathered browse requests.
SEARCHES = ["artist hits vol. 2", "artist hits volume 2"]
# What master sends for this release: one index, however many first requests go out together
# (#468), the search, the request search and the group shown after the upload (the upload itself
# is stubbed out).
REQUESTS_WHEN_A_GROUP_IS_FOUND = [
    "index",
    *(f"browse {s}" for s in SEARCHES),
    *(f"requests {s}" for s in SEARCHES),
    "torrentgroup 5",
]
# The fake's log pages have no rows, so no time to say they cover the window: the log is read up to its third page.
LOG_PAGES = [f"log {page}" for page in range(1, 4)]


class CountingLimiter(AsyncLimiter):
    def __init__(self, max_rate: float, time_period: float) -> None:
        super().__init__(max_rate, time_period)
        self.acquired = 0

    async def acquire(self, amount: float = 1) -> None:
        await super().acquire(amount)
        self.acquired += 1


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
    monkeypatch.setattr(salmon.uploader.cfg.upload.requests, "confirm_new_group", True)

    anyio.run(flow.run)

    assert Counter(tracker.hits) == Counter(REQUESTS_WHEN_A_GROUP_IS_FOUND + LOG_PAGES)
    assert flow.events == ["checks start", "checks end", "group prompt", "spectral step", "upload"]
    assert _during_the_checks(capsys.readouterr().out) == ""


def test_nothing_found_by_the_search_and_the_log_skips_the_question_with_the_same_requests(monkeypatch, capsys) -> None:
    tracker = FakeTracker(browse_results=[])
    flow = Flow(monkeypatch, tracker)
    monkeypatch.setattr(salmon.uploader.cfg.upload.requests, "confirm_new_group", False)

    anyio.run(flow.run)

    assert Counter(tracker.hits) == Counter(REQUESTS_WHEN_A_GROUP_IS_FOUND + LOG_PAGES)
    assert flow.events == ["checks start", "checks end", "spectral step", "upload"]
    out = capsys.readouterr().out
    assert _during_the_checks(out) == ""
    assert "No group found on RED: new group" in _before_the_spectral_step(out)


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

    with pytest.raises(click.exceptions.Exit):
        anyio.run(flow.run)

    assert flow.events == ["checks start", "checks end"]
    out = capsys.readouterr().out
    assert _during_the_checks(out) == ""
    assert "Request to RED failed (400)" in _before_the_spectral_step(out)
    assert "Could not search RED for dupes: " in _before_the_spectral_step(out)
    assert "bad search" in _before_the_spectral_step(out)


def test_an_expired_cookie_is_reported_before_the_spectral_step_as_before(monkeypatch, capsys) -> None:
    tracker = FakeTracker(browse_results=[])
    flow = Flow(monkeypatch, tracker)

    with pytest.raises(click.exceptions.Exit):
        anyio.run(lambda: flow.run(cookie="expired"))

    assert tracker.hits.count("log 1") == 1
    assert "login" not in tracker.hits
    out = capsys.readouterr().out
    assert flow.events == ["checks start", "checks end"]
    assert _during_the_checks(out) == ""
    assert "sent this request to its login page" in _before_the_spectral_step(out)
    assert "Could not search RED for dupes: " in _before_the_spectral_step(out)


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
