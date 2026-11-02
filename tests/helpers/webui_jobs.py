"""Helpers of tests/test_webui_jobs.py that other test modules use too."""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import msgspec
from aiohttp.test_utils import TestClient, TestServer

from salmon.trackers import account
from salmon.webui import server
from salmon.webui.jobs import FINISHED, Job, JobManager

TOKEN = "correct-horse-battery-staple-0123456789"


AUTH = {"Authorization": f"Bearer {TOKEN}"}


Run = Callable[["Params"], Awaitable[Any]]


class Params(msgspec.Struct):
    name: str = ""
    folder: str | None = None


def _with_app(test: Callable[[TestClient, JobManager], Awaitable[None]], max_jobs: int = 2) -> None:
    async def main() -> None:
        app = server.create_app(TOKEN, "127.0.0.1", [], max_jobs=max_jobs)
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            await test(client, app[server.JOBS])
        # Every job ended with the server, and the request loop is free again.
        assert all(job.status in FINISHED for job in app[server.JOBS].jobs.values())
        assert account._request_loop is None

    anyio.run(main)


async def _start(client: TestClient, kind: str, **body: Any) -> str:
    params = {key: body.pop(key) for key in ("name", "folder") if key in body}
    response = await client.post("/api/jobs", json={"kind": kind, "params": params, **body}, headers=AUTH)
    assert response.status == 201, await response.text()
    return (await response.json())["id"]


async def _until(predicate: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def _finished(manager: JobManager, job_id: str) -> Job:
    await _until(lambda: manager.jobs[job_id].status in FINISHED)
    return manager.jobs[job_id]


async def _question(manager: JobManager, job_id: str) -> dict[str, Any]:
    await _until(lambda: manager.jobs[job_id].question is not None or manager.jobs[job_id].status in FINISHED)
    question = manager.jobs[job_id].question
    assert question is not None, manager.jobs[job_id].error
    return question


async def _answer(client: TestClient, manager: JobManager, job_id: str, value: Any) -> dict[str, Any]:
    """Answer the job's open question; return that question."""
    question = await _question(manager, job_id)
    response = await client.post(
        f"/api/jobs/{job_id}/answer", json={"question_id": question["id"], "value": value}, headers=AUTH
    )
    assert response.status == 200, await response.text()
    await _until(lambda: (manager.jobs[job_id].question or {}).get("id") != question["id"])
    return question


def _log(job: Job) -> list[str]:
    return [line["text"] for line in job.log]


def _gate() -> tuple[threading.Event, Run]:
    """A job that runs until the test opens its gate."""
    gate = threading.Event()

    async def run(_params: Params) -> str:
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return "through"

    return gate, run


SECRETS = {
    "session": "planted-session-cookie-2b1f",
    "api_key": "planted-red-api-key-77c0",
    "image_key": "planted-imgbb-key-913e",
    "client_password": "planted-client-password-5a2d",
    "rclone_password": "planted-rclone-pass-0c4b",
    "discogs": "planted-discogs-token-e81f",
    "authkey": "plantedauthkey0123456789abcdef",
    "passkey": "plantedpasskey0123456789abcdef",
    "token": TOKEN,
}
