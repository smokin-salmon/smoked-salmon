"""Helpers of tests/test_webui_upload.py that other test modules use too."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient

from helpers.uploader_dry_run import (
    SOURCE_URL,
    FakeTracker,
)
from helpers.webui_jobs import AUTH, _finished, _until, _with_app
from salmon.webui.jobs import FINISHED, Job, JobManager

# What _run_up gives salmon up besides the album: -t RED (unless a test names its own), -s WEB -n -su
# --skip-integrity-check --skip-up.
PARAMS = {
    "source": "WEB",
    "trackers": ["RED"],
    "auto_rename": True,
    "skip_integrity_check": True,
    "skip_up": True,
    "source_url": SOURCE_URL,
}


async def _post_upload(client: TestClient, path: Path | str, **body: Any) -> Any:
    params = {"path": str(path), **PARAMS, **body.pop("params", {})}
    return await client.post("/api/jobs", json={"kind": "upload", "params": params, **body}, headers=AUTH)


async def _start(client: TestClient, path: Path | str, **body: Any) -> str:
    response = await _post_upload(client, path, **body)
    assert response.status == 201, await response.text()
    return (await response.json())["id"]


async def _drive(
    client: TestClient, manager: JobManager, job_id: str, answer: Callable[[dict[str, Any]], Any] = lambda _q: ""
) -> tuple[Job, list[dict[str, Any]]]:
    """Answer each question the job asks with `answer(question)` (empty: the default), until it ends."""
    asked: list[dict[str, Any]] = []
    while True:
        await _until(
            lambda: (
                manager.jobs[job_id].status in FINISHED
                or (manager.jobs[job_id].question is not None and manager.jobs[job_id].question not in asked)
            )
        )
        job = manager.jobs[job_id]
        question = job.question
        if job.status in FINISHED or question is None:
            return await _finished(manager, job_id), asked
        asked.append(question)
        response = await client.post(
            f"/api/jobs/{job_id}/answer", json={"question_id": question["id"], "value": answer(question)}, headers=AUTH
        )
        assert response.status == 200, await response.text()


def _web_run(
    tracker: FakeTracker, test: Callable[[TestClient, JobManager], Awaitable[None]], max_jobs: int = 2
) -> None:
    """Run `test` against salmon web, with the fake tracker serving on the server's loop."""

    async def serving(client: TestClient, manager: JobManager) -> None:
        async with tracker.serving():
            await test(client, manager)

    _with_app(serving, max_jobs=max_jobs)
