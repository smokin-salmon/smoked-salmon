"""What a salmon web upload job ends as, through the HTTP API on the local fake tracker (#943).

As the cross-upload job: a job that stops says so and lists what went up, and an outcome the run could not tell stays
`unknown_outcome`, whatever stopped the run after it. With nothing up the result stays None.

Each row of AS_BEFORE ends as master does; each row of CHANGED ended with another status or result.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import asyncclick as click
import pytest
from aiohttp.test_utils import TestClient
from helpers.uploader_dry_run import FakeTracker, _album, fake_upload_world
from helpers.webui_jobs import AUTH, _finished, _log, _until
from helpers.webui_upload import _drive, _start, _web_run
from test_run_exit_status import (
    BAD_CLIENT,
    TWO,
    Answer,
    Lost,
    LostNth,
    RefuseNth,
    _a_client_add_raises,
    _at,
    _at_ops,
    _cover_on,
    _forbidden_on,
    _lossy_report_raises,
    _seedbox,
    _transcode_but_v0,
    no_waits,  # noqa: F401
)
from test_uploader_dry_run import image_uploads  # noqa: F401
from test_uploader_run_trackers import RefusingTracker
from test_webui_upload import no_learned_secrets, roots  # noqa: F401

from salmon.errors import RequestFailedError, UnknownOutcomeError, UploadError
from salmon.webui.jobs import JobManager

pytestmark = pytest.mark.usefixtures("image_uploads")

STOPPED = "The run stopped before everything went up"
A_BUG = RuntimeError("a bug")
NOT_MADE = UploadError("Not overwriting existing folder.")
STOP = UploadError("stopped")


def _pick(letter: str) -> dict[str, Any]:
    """The answer to a described question: the pick, as the page sends it."""
    return {"picks": [letter], "texts": [], "mark": None}


@dataclass(frozen=True)
class Row:
    status: str
    # How many torrents the job's result lists; None: it has no result.
    uploads: int | None
    # In the job's error.
    said: str | None = None
    trackers: tuple[str, ...] = ("RED",)
    params: dict[str, Any] = field(default_factory=dict)
    tracker: Callable[[], FakeTracker] = FakeTracker
    fakes: Callable[[], dict[str, Any]] = dict
    setup: Callable[..., None] | None = None
    dry_run: bool = False
    # The answers to the questions the job asks, in turn; then the default. Starts it with assume_defaults.
    answers: tuple[Any, ...] = ()
    # The result says a seedbox step (a copy, a client add, or setting up the client) failed, for the page's warning.
    seedbox_failed: bool = False
    # The steps its result names (a run queues several folders and torrents), each on the seedbox `box`.
    seedbox_steps: frozenset[str] = frozenset()


V0 = {"conversions": ["V0"]}

CHANGED = {
    # Stopped with nothing up: done, on master
    "refused": Row("failed", None, STOPPED, tracker=lambda: RefusingTracker("RED")),
    "mqa": Row("failed", None, STOPPED, fakes=_at("mqa_test", click.Abort)),
    "only_tracker_forbids": Row("failed", None, STOPPED, fakes=lambda: _forbidden_on("RED")),
    "dry_run_stopped": Row("failed", None, STOPPED, dry_run=True, fakes=_at("mqa_test", click.Abort)),
    # Stopped with something up: done, with it listed, on master
    "red_up_ops_refused": Row("failed", 3, STOPPED, TWO, tracker=lambda: RefusingTracker("OPS")),
    "red_refused_ops_up": Row("failed", 3, STOPPED, TWO, tracker=lambda: RefusingTracker("RED")),
    "red_up_ops_forbids": Row("failed", 3, STOPPED, TWO, fakes=lambda: _forbidden_on("OPS")),
    "red_up_abort_on_ops_cover": Row("failed", 3, STOPPED, TWO, fakes=lambda: _cover_on("OPS", click.Abort)),
    "conversion_refused": Row("failed", 1, STOPPED, params=V0, tracker=lambda: RefuseNth(2)),
    "conversion_not_made": Row("failed", 1, STOPPED, params=V0, fakes=_at("transcode_folder", NOT_MADE)),
    "one_conversion_of_two_not_made": Row(
        "failed",
        2,
        STOPPED,
        params={"conversions": ["V0", "320"]},
        fakes=lambda: {"transcode_folder": _transcode_but_v0},
    ),
    # Failed with something up: no result, on master
    "red_up_upload_error_on_ops": Row("failed", 3, "stopped", TWO, fakes=lambda: _cover_on("OPS", STOP)),
    "red_up_bug_on_ops": Row("failed", 3, "RuntimeError: a bug", TWO, fakes=_at_ops("check_requests", A_BUG)),
    # An outcome it could not tell with something up: no result, on master
    "red_up_ops_lost": Row("unknown_outcome", 3, "check your uploads on OPS", TWO, tracker=lambda: Lost("OPS")),
    "red_lost_ops_up": Row("unknown_outcome", 3, "check your uploads on RED", TWO, tracker=lambda: Lost("RED")),
    "conversion_lost": Row("unknown_outcome", 1, "check your uploads on RED", params=V0, tracker=lambda: LostNth(2)),
    "lossy_report_lost": Row(
        "unknown_outcome", 3, "lost report", setup=_lossy_report_raises(UnknownOutcomeError("lost report"))
    ),
    # The status stays unknown_outcome whatever stops the run after it
    "conversion_lost_then_upload_error": Row(
        "unknown_outcome", 1, "check your uploads on RED", TWO, V0, lambda: LostNth(2), lambda: _cover_on("OPS", STOP)
    ),
    "conversion_lost_then_a_bug": Row(
        "unknown_outcome", 1, "check your uploads on RED", TWO, V0, lambda: LostNth(2), _at_ops("check_requests", A_BUG)
    ),
    "conversion_lost_then_an_abort": Row(
        "unknown_outcome",
        1,
        "check your uploads on RED",
        TWO,
        V0,
        lambda: LostNth(2),
        lambda: _cover_on("OPS", click.Abort),
    ),
    # Up, and a seedbox failed: what the CLI exits 3 for. Done, with nothing in the result to say so, on master
    "seedbox_copy_failed": Row(
        "done",
        3,
        setup=_seedbox(copy_exit=1),
        seedbox_failed=True,
        seedbox_steps=frozenset({"copy", "add skipped (copy failed)"}),
    ),
    "seedbox_client_add_raises": Row(
        "done", 3, setup=_a_client_add_raises, seedbox_failed=True, seedbox_steps=frozenset({"client add"})
    ),
    "seedbox_never_configured": Row(
        "done",
        3,
        setup=_seedbox(clients=(BAD_CLIENT,)),
        seedbox_failed=True,
        seedbox_steps=frozenset({"client not set up"}),
    ),
}

AS_BEFORE = {
    # All it set out to upload went up
    "one_tracker": Row("done", 3),
    "two_trackers": Row("done", 6, trackers=TWO),
    "one_conversion": Row("done", 2, params=V0),
    "dry_run": Row("done", 0, dry_run=True),
    "skip_ops_at_its_group_question": Row("done", 3, trackers=TWO, answers=("", _pick("s"))),
    # Up, and a side step failed: the log says what to do by hand
    "fill_refused": Row("done", 3, params={"request": "4242"}, tracker=lambda: Answer(groupid=7, requestid=-1)),
    "lossy_report_refused": Row("done", 3, setup=_lossy_report_raises(RequestFailedError("no report"))),
    # Up, and the seedbox took it
    "seedbox_ok": Row("done", 3, setup=_seedbox()),
    # Stopped with nothing up: the error, and no result
    "upload_error": Row("failed", None, "stopped", fakes=lambda: _cover_on("RED", STOP)),
    "a_bug": Row("failed", None, "RuntimeError: a bug", fakes=_at("check_requests", A_BUG)),
    "lost": Row("unknown_outcome", None, "check your uploads on RED", tracker=lambda: Lost("RED")),
    "both_lost": Row("unknown_outcome", None, "check your uploads on OPS", TWO, tracker=Lost),
    "lost_then_upload_error": Row(
        "unknown_outcome",
        None,
        "check your uploads on RED",
        TWO,
        tracker=lambda: Lost("RED"),
        fakes=lambda: _cover_on("OPS", STOP),
    ),
    "lost_then_a_bug": Row(
        "unknown_outcome",
        None,
        "check your uploads on RED",
        TWO,
        tracker=lambda: Lost("RED"),
        fakes=_at_ops("check_requests", A_BUG),
    ),
}


def _uploads_listed(job: Any) -> int | None:
    return None if job.result is None else len(job.result["uploads"])


def _check(monkeypatch, roots, row: Row) -> None:  # noqa: F811
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    tracker = row.tracker()
    fake_upload_world(monkeypatch, torrents, tracker=tracker, **row.fakes())
    if row.setup is not None:
        row.setup(monkeypatch, album)
    answers = iter(row.answers)

    async def test(client: TestClient, manager: JobManager) -> None:
        body: dict[str, Any] = {"params": {"trackers": list(row.trackers), **row.params}}
        if row.dry_run:
            body["dry_run"] = True
        if row.answers:
            body["assume_defaults"] = True
        job, _asked = await _drive(client, manager, await _start(client, album, **body), lambda _q: next(answers, ""))
        assert (job.status, _uploads_listed(job)) == (row.status, row.uploads), (job.error, _log(job))
        if job.result is not None:
            assert (job.result["folder"], job.result["trackers"]) == ("Album", list(row.trackers))
            assert job.result.get("seedbox_failed", False) is row.seedbox_failed
            failures = job.result.get("seedbox_failures", [])
            assert {f["seedbox"] for f in failures} <= {"box"}
            assert {f["step"] for f in failures} == row.seedbox_steps, failures
            assert all(f["item"] and os.path.basename(f["item"]) == f["item"] for f in failures), failures
        if row.said is None:
            assert job.error is None
        else:
            assert job.error is not None
            assert row.said in job.error

    _web_run(tracker, test)


@pytest.mark.parametrize("name", CHANGED)
def test_an_upload_job_ends(monkeypatch, roots, name: str) -> None:  # noqa: F811
    _check(monkeypatch, roots, CHANGED[name])


@pytest.mark.parametrize("name", AS_BEFORE)
def test_as_before_an_upload_job_ends(monkeypatch, roots, name: str) -> None:  # noqa: F811
    _check(monkeypatch, roots, AS_BEFORE[name])


def _cancelled_once_up(monkeypatch, roots, up: int) -> Any:  # noqa: F811
    """A RED and OPS job, cancelled at its first question with `up` torrents uploaded."""
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents)
    ended: list[Any] = []

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, album, params={"trackers": list(TWO)})
        job = manager.jobs[job_id]
        answered: list[Any] = []
        while True:
            await _until(lambda: job.question is not None and job.question not in answered)
            if sum(line.startswith("Successfully uploaded") for line in _log(job)) == up:
                break
            answered.append(job.question)
            assert job.question is not None
            response = await client.post(
                f"/api/jobs/{job_id}/answer", json={"question_id": job.question["id"], "value": ""}, headers=AUTH
            )
            assert response.status == 200
        await client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=AUTH)
        ended.append(await _finished(manager, job_id))

    _web_run(tracker, test)
    return ended[0]


def test_a_job_cancelled_with_something_up_lists_it(monkeypatch, roots) -> None:  # noqa: F811
    job = _cancelled_once_up(monkeypatch, roots, 3)
    assert (job.status, _uploads_listed(job)) == ("cancelled", 3)


def test_as_before_a_job_cancelled_with_nothing_up_has_no_result(monkeypatch, roots) -> None:  # noqa: F811
    job = _cancelled_once_up(monkeypatch, roots, 0)
    assert (job.status, job.result) == ("cancelled", None)
