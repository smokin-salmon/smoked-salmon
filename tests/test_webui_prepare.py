"""salmon web prepares a queued upload while it waits: its local checks run before its turn (#790).

The queue's own rules are tested with job kinds of the tests' own, whose preparation the test controls; the upload
kind's preparation runs against the upload tests' fake tracker, which must see no request from it.
"""

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import msgspec
import pytest
from aiohttp.test_utils import TestClient
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    _album,
    _snapshot,
    fake_upload_world,
    image_uploads,  # noqa: F401
)
from test_webui_jobs import AUTH, _finished, _gate, _log, _until, _with_app  # pyright: ignore[reportMissingImports]
from test_webui_upload import _drive, _start, _web_run, roots  # noqa: F401  # pyright: ignore[reportMissingImports]

from salmon import cfg, prepared
from salmon.prepared import Prepared
from salmon.uploader import spectrals
from salmon.webui import jobs
from salmon.webui.jobs import JobKind, JobManager


class Params(msgspec.Struct):
    name: str = ""
    folder: str | None = None


Prepare = Callable[[Params, Prepared], Awaitable[None]]


@pytest.fixture
def kinds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., None]:
    """Register job kinds for the test: run, and prepare when given. Every job works on a folder of its own."""
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)

    def register(name: str, run: Callable[[Params], Awaitable[Any]], prepare: Prepare | None = None) -> None:
        monkeypatch.setitem(
            jobs.KINDS,
            name,
            JobKind(
                name=name,
                params=Params,
                run=run,
                title=lambda p: f"{name} {p.name}",
                folder=lambda p: p.folder,
                prepare=prepare,
            ),
        )

    return register


async def _start_job(client: TestClient, kind: str, folder: Path, name: str = "") -> str:
    folder.mkdir(exist_ok=True)
    response = await client.post(
        "/api/jobs", json={"kind": kind, "params": {"name": name, "folder": str(folder)}}, headers=AUTH
    )
    assert response.status == 201, await response.text()
    return (await response.json())["id"]


async def _status(client: TestClient, job_id: str) -> str:
    response = await client.get(f"/api/jobs/{job_id}", headers=AUTH)
    return (await response.json())["status"]


def _seen(manager: JobManager) -> list[tuple[str, str]]:
    """Every status event the manager publishes from now on, as (job id, status)."""
    events: list[tuple[str, str]] = []
    publish = manager._publish

    def recording(event: dict[str, Any]) -> None:
        if event["event"] == "status":
            events.append((event["job_id"], event["status"]))
        publish(event)

    manager._publish = recording  # type: ignore[method-assign]
    return events


def test_a_queued_job_is_prepared_while_it_waits_and_runs_with_what_was_prepared(kinds, tmp_path) -> None:
    gate, gated = _gate()
    seen_by_the_run: list[Any] = []

    async def prepare(params: Params, made: Prepared) -> None:
        assert params.folder is not None
        track = os.path.join(params.folder, "01.flac")
        Path(track).write_bytes(b"audio")
        made.keep("mqa", track, os.stat(track), False)

    async def run(params: Params) -> str:
        assert params.folder is not None
        seen_by_the_run.append(prepared.result("mqa", params.folder, os.path.join(params.folder, "01.flac")))
        return "ran"

    kinds("gated", gated)
    kinds("prepared", run, prepare)

    async def test(client: TestClient, manager: JobManager) -> None:
        statuses = _seen(manager)
        await _start_job(client, "gated", tmp_path / "a")
        waiting = await _start_job(client, "prepared", tmp_path / "b")
        await _until(lambda: manager.jobs[waiting].status == "ready")
        assert await _status(client, waiting) == "ready"
        assert [status for job, status in statuses if job == waiting] == ["preparing", "ready"]
        gate.set()
        assert (await _finished(manager, waiting)).status == "done"
        assert statuses[-1] == (waiting, "running")

    _with_app(test, max_jobs=1)
    assert seen_by_the_run == [False]


def test_a_job_that_starts_at_once_is_not_prepared(kinds, tmp_path) -> None:
    prepared_jobs: list[str] = []

    async def prepare(params: Params, _made: Prepared) -> None:
        prepared_jobs.append(params.name)

    async def run(_params: Params) -> str:
        return "ran"

    kinds("prepared", run, prepare)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _start_job(client, "prepared", tmp_path / "a")
        assert (await _finished(manager, job)).status == "done"

    _with_app(test, max_jobs=1)
    assert prepared_jobs == []


def test_queued_jobs_are_prepared_one_at_a_time_in_queue_order(kinds, tmp_path) -> None:
    gate, gated = _gate()
    running: list[str] = []
    most = 0
    order: list[str] = []

    async def prepare(params: Params, _made: Prepared) -> None:
        nonlocal most
        running.append(params.name)
        most = max(most, len(running))
        order.append(params.name)
        await asyncio.sleep(0.2)
        running.remove(params.name)

    async def run(_params: Params) -> str:
        return "ran"

    kinds("gated", gated)
    kinds("prepared", run, prepare)

    async def test(client: TestClient, manager: JobManager) -> None:
        await _start_job(client, "gated", tmp_path / "gated")
        queued = [await _start_job(client, "prepared", tmp_path / name, name) for name in ("one", "two", "three")]
        await _until(lambda: all(manager.jobs[job].status == "ready" for job in queued))
        gate.set()
        for job in queued:
            assert (await _finished(manager, job)).status == "done"

    _with_app(test, max_jobs=1)
    assert order == ["one", "two", "three"]
    assert most == 1


def _endless(kept: threading.Event, stopped: threading.Event) -> Prepare:
    """A preparation that keeps one result, writes into its folder, then runs until it is cancelled."""

    async def prepare(params: Params, made: Prepared) -> None:
        assert params.folder is not None
        track = os.path.join(params.folder, "01.flac")
        Path(track).write_bytes(b"audio")
        made.keep("mqa", track, os.stat(track), True)
        Path(made.folder, "01 Full.png").write_bytes(b"png")
        kept.set()
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    return prepare


def test_a_job_whose_turn_comes_stops_its_preparation_and_takes_what_it_made(kinds, tmp_path) -> None:
    gate, gated = _gate()
    kept, stopped = threading.Event(), threading.Event()
    seen_by_the_run: list[Any] = []

    async def run(params: Params) -> str:
        assert params.folder is not None
        seen_by_the_run.append(prepared.result("mqa", params.folder, os.path.join(params.folder, "01.flac")))
        return "ran"

    kinds("gated", gated)
    kinds("prepared", run, _endless(kept, stopped))

    async def test(client: TestClient, manager: JobManager) -> None:
        await _start_job(client, "gated", tmp_path / "a")
        waiting = await _start_job(client, "prepared", tmp_path / "b")
        await _until(kept.is_set)
        assert manager.jobs[waiting].status == "preparing"
        gate.set()
        assert (await _finished(manager, waiting)).status == "done"
        await _until(stopped.is_set)

    _with_app(test, max_jobs=1)
    assert seen_by_the_run == [True]


def test_cancelling_a_job_being_prepared_stops_its_preparation_and_removes_its_folder(kinds, tmp_path) -> None:
    gate, gated = _gate()
    kept, stopped = threading.Event(), threading.Event()
    kinds("gated", gated)
    kinds("prepared", gated, _endless(kept, stopped))

    async def test(client: TestClient, manager: JobManager) -> None:
        await _start_job(client, "gated", tmp_path / "a")
        waiting = await _start_job(client, "prepared", tmp_path / "b")
        await _until(kept.is_set)
        job = manager.jobs[waiting]
        assert job.prepared is not None
        folder = job.prepared.folder
        assert os.path.isdir(folder)
        response = await client.post(f"/api/jobs/{waiting}/cancel", json={}, headers=AUTH)
        assert response.status == 200
        assert (await _finished(manager, waiting)).status == "cancelled"
        await _until(stopped.is_set)
        await _until(lambda: not os.path.exists(folder))
        gate.set()

    _with_app(test, max_jobs=1)


def test_the_preparations_folder_is_removed_when_its_job_ends(kinds, tmp_path) -> None:
    gate, gated = _gate()

    async def prepare(_params: Params, made: Prepared) -> None:
        Path(made.folder, "01 Full.png").write_bytes(b"png")

    async def run(_params: Params) -> str:
        return "ran"

    kinds("gated", gated)
    kinds("prepared", run, prepare)

    async def test(client: TestClient, manager: JobManager) -> None:
        await _start_job(client, "gated", tmp_path / "a")
        waiting = await _start_job(client, "prepared", tmp_path / "b")
        await _until(lambda: manager.jobs[waiting].status == "ready")
        prepared_job = manager.jobs[waiting].prepared
        assert prepared_job is not None
        folder = prepared_job.folder
        # Outside the album, beside the jobs' own folders.
        assert os.path.basename(folder).startswith(f"{jobs.OWN_FOLDER_PREFIX}{waiting}-prepared-")
        assert not folder.startswith(str(tmp_path / "b"))
        gate.set()
        await _finished(manager, waiting)
        await _until(lambda: not os.path.exists(folder))

    _with_app(test, max_jobs=1)


def test_stopping_the_server_stops_a_preparation_and_removes_its_folder(kinds, tmp_path) -> None:
    gate, gated = _gate()
    kept, stopped = threading.Event(), threading.Event()
    folders: list[str] = []
    kinds("gated", gated)
    kinds("prepared", gated, _endless(kept, stopped))

    async def test(client: TestClient, manager: JobManager) -> None:
        await _start_job(client, "gated", tmp_path / "a")
        waiting = await _start_job(client, "prepared", tmp_path / "b")
        await _until(kept.is_set)
        job = manager.jobs[waiting]
        assert job.prepared is not None
        folders.append(job.prepared.folder)

    _with_app(test, max_jobs=1)
    assert stopped.wait(5)
    assert folders and not os.path.exists(folders[0])


def test_what_a_preparation_prints_goes_nowhere(kinds, tmp_path, capfd) -> None:
    gate, gated = _gate()

    async def prepare(_params: Params, _made: Prepared) -> None:
        print("printed while preparing")

    async def run(_params: Params) -> str:
        return "ran"

    kinds("gated", gated)
    kinds("prepared", run, prepare)

    async def test(client: TestClient, manager: JobManager) -> None:
        await _start_job(client, "gated", tmp_path / "a")
        waiting = await _start_job(client, "prepared", tmp_path / "b")
        await _until(lambda: manager.jobs[waiting].status == "ready")
        gate.set()
        job = await _finished(manager, waiting)
        assert not [line for line in _log(job) if "printed while preparing" in line]

    _with_app(test, max_jobs=1)
    out, err = capfd.readouterr()
    assert "printed while preparing" not in out + err


def test_a_preparation_that_fails_leaves_the_job_to_run_as_it_would(kinds, tmp_path) -> None:
    gate, gated = _gate()

    async def prepare(_params: Params, _made: Prepared) -> None:
        raise RuntimeError("no sox")

    async def run(_params: Params) -> str:
        return "ran"

    kinds("gated", gated)
    kinds("prepared", run, prepare)

    async def test(client: TestClient, manager: JobManager) -> None:
        await _start_job(client, "gated", tmp_path / "a")
        waiting = await _start_job(client, "prepared", tmp_path / "b")
        await _until(lambda: manager.jobs[waiting].status == "ready")
        gate.set()
        job = await _finished(manager, waiting)
        assert (job.status, job.result) == ("done", "ran")

    _with_app(test, max_jobs=1)


# --- The upload kind -------------------------------------------------------------------------------


@pytest.fixture
def fake_sox(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """sox, where salmon calls it: writes the images asked for, each run recorded by its input file."""
    runs: list[str] = []
    run_process = spectrals.anyio.run_process

    async def sox(args: list[str], **kwargs: Any) -> Any:
        if args[0] != "sox":
            return await run_process(args, **kwargs)
        runs.append(os.path.basename(args[2]))
        for i, arg in enumerate(args):
            if arg == "-o":
                Path(args[i + 1]).write_bytes(b"\x89PNG spectral")
        return None

    monkeypatch.setattr(spectrals.anyio, "run_process", sox)
    return runs


@pytest.mark.usefixtures("image_uploads")
def test_a_queued_upload_is_prepared_without_a_request_or_a_write_into_the_album(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],  # noqa: F811
    fake_sox: list[str],
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    before = _snapshot(album)
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents)
    gate, gated = _gate()
    monkeypatch.setitem(jobs.KINDS, "gated", JobKind(name="gated", params=Params, run=gated, title=lambda _p: "gated"))

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await client.post("/api/jobs", json={"kind": "gated", "params": {}}, headers=AUTH)
        assert response.status == 201
        upload = await _start(client, album)
        await _until(lambda: manager.jobs[upload].status == "ready")
        job = manager.jobs[upload]
        assert job.prepared is not None
        assert job.prepared.checks() >= {"spectral"}
        assert sorted(fake_sox) == ["01 - one.flac", "02 - two.flac"]
        # Preparing sent nothing and changed nothing in the album.
        assert tracker.sent == []
        assert _snapshot(album) == before
        gate.set()
        done, _asked = await _drive(client, manager, upload)
        assert done.status == "done", (done.error, _log(done))
        assert tracker.sent != []

    _web_run(tracker, test, max_jobs=1)
