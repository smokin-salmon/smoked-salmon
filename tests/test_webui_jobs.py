"""salmon web's jobs: threads, the queue, questions in the browser, output, egress redaction (#631, ADR 0004).

Every test runs the server on 127.0.0.1 with aiohttp's test server, and job kinds of its own: no real job kind is
exposed yet. The tests that send tracker requests send them to a local fake tracker.
"""

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import msgspec
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from test_trackers_account import (  # pyright: ignore[reportMissingImports]
    FakeTracker,
    _clients,
    _get,
    _most_within,
    _post,
)

from salmon import cfg, dryrun, interaction
from salmon.config.validations import GazelleTrackerSettings, Seedbox
from salmon.errors import UnknownOutcomeError
from salmon.trackers import account, base
from salmon.webui import egress, jobs, server
from salmon.webui.jobs import FINISHED, Job, JobKind, JobManager

TOKEN = "correct-horse-battery-staple-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

Run = Callable[["Params"], Awaitable[Any]]


class Params(msgspec.Struct):
    name: str = ""
    folder: str | None = None


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the authkeys and passkeys other tests' fake trackers gave, short ones among them."""
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture
def kind(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, Run], None]:
    """Register a job kind for the test."""

    def register(name: str, run: Run) -> None:
        monkeypatch.setitem(
            jobs.KINDS,
            name,
            JobKind(name=name, params=Params, run=run, title=lambda p: f"{name} {p.name}", folder=lambda p: p.folder),
        )

    return register


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


# --- Starting a job -------------------------------------------------------------------------


def test_a_job_of_a_registered_kind_runs_in_a_thread_of_its_own(kind: Callable[[str, Run], None]) -> None:
    server_thread = threading.get_ident()

    async def run(params: Params) -> dict[str, Any]:
        return {"name": params.name, "own_thread": threading.get_ident() != server_thread}

    kind("thread", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "thread", name="x")
        job = await _finished(manager, job_id)
        assert job.status == "done"
        assert job.result == {"name": "x", "own_thread": True}
        listed = await (await client.get("/api/jobs", headers=AUTH)).json()
        assert [each["id"] for each in listed["jobs"]] == [job_id]
        assert listed["jobs"][0]["title"] == "thread x"

    _with_app(test)


def test_only_the_job_kinds_of_v1_are_exposed() -> None:
    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await client.post("/api/jobs", json={"kind": "cross-upload"}, headers=AUTH)
        assert response.status == 400
        assert "Unknown job kind" in (await response.json())["detail"]

    assert set(jobs.KINDS) == {
        "spectrals",
        "checks",
        "upload",
        "transcode",
        "downconvert",
        "compress",
        "connection_check",
    }
    _with_app(test)


def test_a_job_with_parameters_its_kind_does_not_take_is_refused(kind: Callable[[str, Run], None]) -> None:
    kind("strict", lambda _params: asyncio.sleep(0))

    async def test(client: TestClient, manager: JobManager) -> None:
        for body in ({"kind": "strict", "params": {"name": 3}}, {"kind": "strict", "nothing": 1}, {"params": {}}):
            response = await client.post("/api/jobs", json=body, headers=AUTH)
            assert response.status == 400, body
        assert manager.jobs == {}

    _with_app(test)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/jobs"),
        ("POST", "/api/jobs"),
        ("GET", "/api/jobs/job-1"),
        ("POST", "/api/jobs/job-1/answer"),
        ("POST", "/api/jobs/job-1/cancel"),
        ("POST", "/api/jobs/job-1/discard"),
        ("GET", "/api/jobs/job-1/spectrals/01 Full.png"),
        ("GET", "/api/browse"),
        ("GET", "/api/dashboard"),
        ("GET", "/api/upload/options"),
        ("GET", "/api/ws"),
    ],
)
def test_the_job_routes_need_a_login(method: str, path: str) -> None:
    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await client.request(method, path, json={})
        assert response.status == 401

    _with_app(test)


def test_job_changes_must_be_json_from_this_site(kind: Callable[[str, Run], None]) -> None:
    kind("any", lambda _params: asyncio.sleep(0))

    async def test(client: TestClient, manager: JobManager) -> None:
        not_json = await client.post("/api/jobs", data="kind=any", headers=AUTH)
        assert not_json.status == 415
        elsewhere = await client.post("/api/jobs", json={"kind": "any"}, headers={**AUTH, "Origin": "http://evil.test"})
        assert elsewhere.status == 403
        assert manager.jobs == {}

    _with_app(test)


# --- The queue ----------------------------------------------------------------------------


def test_at_most_max_jobs_run_and_the_rest_wait_in_order(kind: Callable[[str, Run], None]) -> None:
    gates = {name: _gate() for name in "abc"}
    for name, (_, run) in gates.items():
        kind(name, run)

    async def test(client: TestClient, manager: JobManager) -> None:
        a, b, c = [await _start(client, name) for name in "abc"]
        await _until(lambda: manager.jobs[a].status == "running")
        await asyncio.sleep(0.1)
        assert [manager.jobs[each].status for each in (a, b, c)] == ["running", "queued", "queued"]
        gates["a"][0].set()
        await _finished(manager, a)
        await _until(lambda: manager.jobs[b].status == "running")
        assert manager.jobs[c].status == "queued"
        gates["b"][0].set()
        gates["c"][0].set()
        await _finished(manager, c)
        assert [manager.jobs[each].result for each in (a, b, c)] == ["through"] * 3

    _with_app(test, max_jobs=1)


def test_a_second_job_on_the_same_folder_waits_and_another_folder_goes_on(
    kind: Callable[[str, Run], None], tmp_path: Path
) -> None:
    gate, run = _gate()
    kind("gated", run)
    album = tmp_path / "album"
    album.mkdir()
    # The same folder by another path.
    (tmp_path / "link").symlink_to(album)
    other = tmp_path / "other"
    other.mkdir()

    async def test(client: TestClient, manager: JobManager) -> None:
        first = await _start(client, "gated", folder=str(album))
        same = await _start(client, "gated", folder=str(tmp_path / "link"))
        elsewhere = await _start(client, "gated", folder=str(other))
        await _until(lambda: manager.jobs[elsewhere].status == "running")
        assert manager.jobs[first].status == "running"
        assert manager.jobs[same].status == "queued"
        gate.set()
        for each in (first, same, elsewhere):
            assert (await _finished(manager, each)).status == "done"

    _with_app(test, max_jobs=3)


def test_the_queue_is_bounded(kind: Callable[[str, Run], None], monkeypatch: pytest.MonkeyPatch) -> None:
    gate, run = _gate()
    kind("gated", run)
    monkeypatch.setattr(jobs, "MAX_QUEUED_JOBS", 2)

    async def test(client: TestClient, _manager: JobManager) -> None:
        for _ in range(3):
            await _start(client, "gated")
        response = await client.post("/api/jobs", json={"kind": "gated"}, headers=AUTH)
        assert response.status == 429
        gate.set()

    _with_app(test, max_jobs=1)


def test_finished_jobs_are_dropped_oldest_first(
    kind: Callable[[str, Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    kind("quick", lambda _params: asyncio.sleep(0))
    monkeypatch.setattr(jobs, "MAX_FINISHED_JOBS", 2)

    async def test(client: TestClient, manager: JobManager) -> None:
        ids = []
        for _ in range(4):
            ids.append(await _start(client, "quick"))
            await _finished(manager, ids[-1])
        await _start(client, "quick")
        assert ids[0] not in manager.jobs
        assert ids[1] not in manager.jobs
        assert (await client.get(f"/api/jobs/{ids[0]}", headers=AUTH)).status == 404

    _with_app(test)


# --- Cancelling ---------------------------------------------------------------------------


def test_cancelling_a_queued_job_and_a_running_one(kind: Callable[[str, Run], None]) -> None:
    gate, run = _gate()
    kind("gated", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        running = await _start(client, "gated")
        queued = await _start(client, "gated")
        await _until(lambda: manager.jobs[running].status == "running")
        for each in (queued, running):
            response = await client.post(f"/api/jobs/{each}/cancel", json={}, headers=AUTH)
            assert response.status == 200
        assert (await _finished(manager, queued)).status == "cancelled"
        assert (await _finished(manager, running)).status == "cancelled"
        again = await client.post(f"/api/jobs/{running}/cancel", json={}, headers=AUTH)
        assert again.status == 409
        assert (await client.post("/api/jobs/job-0/cancel", json={}, headers=AUTH)).status == 404

    _with_app(test, max_jobs=1)


def test_cancelling_a_job_waiting_for_an_answer(kind: Callable[[str, Run], None]) -> None:
    async def run(_params: Params) -> bool:
        return await interaction.confirm("Go on?")

    kind("asks", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "asks")
        await _question(manager, job_id)
        assert manager.jobs[job_id].status == "waiting"
        await client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=AUTH)
        job = await _finished(manager, job_id)
        assert job.status == "cancelled"
        assert job.question is None

    _with_app(test)


def test_stopping_the_server_cancels_its_jobs(kind: Callable[[str, Run], None]) -> None:
    _gate_event, run = _gate()
    kind("gated", run)
    managers: list[JobManager] = []

    async def test(client: TestClient, manager: JobManager) -> None:
        managers.append(manager)
        await _start(client, "gated")
        await _start(client, "gated")

    _with_app(test, max_jobs=1)
    assert sorted(job.status for job in managers[0].jobs.values()) == ["cancelled", "cancelled"]


# --- Outcomes -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        UnknownOutcomeError("Network error: Server disconnected"),
        ExceptionGroup("group", [UnknownOutcomeError("Server error 502")]),
        RuntimeError("upload failed"),
    ],
)
def test_an_unknown_outcome_gets_a_status_of_its_own(kind: Callable[[str, Run], None], error: Exception) -> None:
    async def run(_params: Params) -> None:
        if isinstance(error, RuntimeError):
            # Raised from an unknown outcome.
            try:
                raise UnknownOutcomeError("Network error: timed out")
            except UnknownOutcomeError as cause:
                raise error from cause
        raise error

    kind("unknown", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "unknown"))
        assert job.status == "unknown_outcome"
        assert job.error is not None
        assert job.error.startswith("The request may have reached the tracker: check the site before trying again.")

    _with_app(test)


def test_a_failed_job_says_why_and_its_traceback_goes_to_the_servers_stderr(
    kind: Callable[[str, Run], None], capfd: pytest.CaptureFixture[str]
) -> None:
    async def run(_params: Params) -> None:
        click.echo("working")
        raise RuntimeError("it broke")

    kind("fails", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "fails"))
        assert job.status == "failed"
        assert job.error == "RuntimeError: it broke"
        assert _log(job) == ["working"]

    _with_app(test)
    err = capfd.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: it broke" in err


def test_an_abort_fails_the_job(kind: Callable[[str, Run], None]) -> None:
    async def run(_params: Params) -> None:
        await interaction.confirm("Upload anyway?", abort=True)

    kind("aborts", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "aborts")
        await _answer(client, manager, job_id, False)
        job = await _finished(manager, job_id)
        assert (job.status, job.error) == ("failed", "Aborted.")

    _with_app(test)


# --- Per-job options ------------------------------------------------------------------------


def test_yes_to_all_and_dry_run_are_each_jobs_own(kind: Callable[[str, Run], None]) -> None:
    async def run(_params: Params) -> dict[str, bool]:
        options = {"assume_defaults": await interaction.assume_defaults(), "dry_run": dryrun.active()}
        if not options["assume_defaults"]:
            options["answer"] = await interaction.confirm("Upload?", default=True)
        return options

    kind("options", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        yes = await _start(client, "options", assume_defaults=True, dry_run=True)
        asks = await _start(client, "options")
        # The job with -yyy is done while the other still waits for its answer.
        assert (await _finished(manager, yes)).result == {"assume_defaults": True, "dry_run": True}
        await _question(manager, asks)
        assert manager.jobs[asks].status == "waiting"
        await _answer(client, manager, asks, True)
        assert (await _finished(manager, asks)).result == {"assume_defaults": False, "dry_run": False, "answer": True}
        assert cfg.upload.yes_all is False

    _with_app(test)


# --- Questions ----------------------------------------------------------------------------


def test_a_prompt_is_asked_in_the_browser_and_a_wrong_answer_asked_again(kind: Callable[[str, Run], None]) -> None:
    async def run(_params: Params) -> list[Any]:
        return [
            await interaction.prompt("How many discs?", type=int),
            await interaction.prompt("Which?", type=click.Choice(["one", "two"])),
            await interaction.prompt("Label", default="none"),
        ]

    kind("prompts", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "prompts")
        first = await _answer(client, manager, job_id, "many")
        assert (first["kind"], first["text"], first["error"]) == ("prompt", "How many discs?", None)
        again = await _answer(client, manager, job_id, "2")
        assert again["text"] == "How many discs?"
        assert again["error"] == "Error: 'many' is not a valid integer."
        choice = await _answer(client, manager, job_id, "three")
        assert choice["choices"] == ["one", "two"]
        await _answer(client, manager, job_id, "two")
        label = await _answer(client, manager, job_id, "")
        assert label["default"] == "none"
        job = await _finished(manager, job_id)
        assert job.result == [2, "two", "none"]
        # As the terminal does, the error is printed too.
        assert "Error: 'many' is not a valid integer." in _log(job)

    _with_app(test)


def test_a_confirm_takes_yes_no_and_the_default_and_asks_again_otherwise(kind: Callable[[str, Run], None]) -> None:
    async def run(_params: Params) -> list[bool]:
        return [
            await interaction.confirm("First?"),
            await interaction.confirm("Second?", default=True),
            await interaction.confirm("Third?", default=None),
        ]

    kind("confirms", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "confirms")
        first = await _answer(client, manager, job_id, True)
        assert (first["kind"], first["default"]) == ("confirm", False)
        await _answer(client, manager, job_id, "")
        third = await _answer(client, manager, job_id, "")
        assert third["default"] is None
        retried = await _answer(client, manager, job_id, "N")
        assert retried["error"] == "Error: invalid input"
        assert (await _finished(manager, job_id)).result == [True, True, False]

    _with_app(test)


def test_edit_sends_the_text_and_gets_it_back(kind: Callable[[str, Run], None]) -> None:
    async def run(_params: Params) -> list[str | None]:
        return [await interaction.edit("old description"), await interaction.edit("keep me")]

    kind("edits", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "edits")
        question = await _answer(client, manager, job_id, "new description")
        assert (question["kind"], question["initial"]) == ("edit", "old description")
        await _answer(client, manager, job_id, None)
        assert (await _finished(manager, job_id)).result == ["new description", None]

    _with_app(test)


def test_spectrals_are_served_from_the_jobs_folder_until_the_user_is_done(
    kind: Callable[[str, Run], None], tmp_path: Path
) -> None:
    spectrals = tmp_path / "Spectrals"
    spectrals.mkdir()
    (spectrals / "01 Full.png").write_bytes(b"\x89PNG full")
    (spectrals / "01 Zoom.png").write_bytes(b"\x89PNG zoom")
    (spectrals / "notes.txt").write_text("not an image")
    (tmp_path / "secret.png").write_bytes(b"\x89PNG outside")
    (spectrals / "02 Full.png").symlink_to(tmp_path / "secret.png")

    async def run(_params: Params) -> str:
        await interaction.show_spectrals(str(spectrals), {1: "01 Track.flac"})
        return "seen"

    kind("spectrals", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "spectrals")
        question = await _question(manager, job_id)
        assert question["kind"] == "spectrals"
        assert question["files"] == ["01 Full.png", "01 Zoom.png"]
        assert question["tracks"] == {"01": "01 Track.flac"}
        image = await client.get(f"/api/jobs/{job_id}/spectrals/01 Full.png", headers=AUTH)
        assert image.status == 200
        assert await image.read() == b"\x89PNG full"
        assert image.content_type == "image/png"
        # A link put in the folder once the images are shown is not followed out of it.
        (spectrals / "01 Zoom.png").unlink()
        (spectrals / "01 Zoom.png").symlink_to(tmp_path / "secret.png")
        for name in ("02 Full.png", "notes.txt", "01 Zoom.png", "..%2Fsecret.png", "%2E%2E%2Fsecret.png"):
            response = await client.get(f"/api/jobs/{job_id}/spectrals/{name}", headers=AUTH)
            assert response.status == 404, name
        await _answer(client, manager, job_id, True)
        assert (await _finished(manager, job_id)).result == "seen"

    _with_app(test)


def test_a_stale_answer_is_refused(kind: Callable[[str, Run], None]) -> None:
    async def run(_params: Params) -> list[bool]:
        return [await interaction.confirm("One?"), await interaction.confirm("Two?")]

    kind("twice", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "twice")
        first = await _answer(client, manager, job_id, True)
        await _question(manager, job_id)
        stale = await client.post(
            f"/api/jobs/{job_id}/answer", json={"question_id": first["id"], "value": False}, headers=AUTH
        )
        assert stale.status == 409
        await _answer(client, manager, job_id, False)
        assert (await _finished(manager, job_id)).result == [True, False]

    _with_app(test)


def test_an_unanswered_question_stops_the_job(
    kind: Callable[[str, Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "QUESTION_TIMEOUT", 0.3)

    async def run(_params: Params) -> bool:
        return await interaction.confirm("Anyone there?")

    kind("waits", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "waits")
        await _question(manager, job_id)
        job = await _finished(manager, job_id)
        assert job.status == "failed"
        assert job.error is not None
        assert job.error.startswith("No answer for")
        assert job.question is None

    _with_app(test)


# --- Output ---------------------------------------------------------------------------------


def test_what_a_job_prints_goes_to_its_log_and_nothing_else_does(
    kind: Callable[[str, Run], None], capsys: pytest.CaptureFixture[str]
) -> None:
    async def run(params: Params) -> None:
        click.echo(f"echo {params.name}")
        click.secho(f"secho {params.name}", fg="green", bold=True)
        click.echo(f"err {params.name}", err=True)
        print(f"print {params.name}")
        click.echo("part, ", nl=False)
        click.echo("whole")
        click.echo("10%\r50%\r100%")
        click.echo("unfinished", nl=False)

    kind("prints", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        print("the server's own line")
        a, b = await _start(client, "prints", name="a"), await _start(client, "prints", name="b")
        for job_id, name in ((a, "a"), (b, "b")):
            job = await _finished(manager, job_id)
            assert [(line["n"], line["text"], line["err"]) for line in job.log] == [
                (1, f"echo {name}", False),
                (2, f"secho {name}", False),
                (3, f"err {name}", True),
                (4, f"print {name}", False),
                (5, "part, whole", False),
                (6, "100%", False),
                (7, "unfinished", False),
            ]

    _with_app(test)
    out = capsys.readouterr().out
    assert "the server's own line" in out
    assert "echo a" not in out


# --- Egress redaction -----------------------------------------------------------------------

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


@pytest.fixture
def planted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config holding a secret of each kind, and an account whose authkey and passkey salmon learned."""
    monkeypatch.setattr(
        cfg.tracker, "red", GazelleTrackerSettings(session=SECRETS["session"], api_key=SECRETS["api_key"])
    )
    monkeypatch.setattr(cfg.image, "imgbb_key", SECRETS["image_key"])
    monkeypatch.setattr(cfg.metadata, "discogs_token", SECRETS["discogs"])
    seedbox = Seedbox(
        name="box",
        url="remote",
        type="rclone",
        extra_args=["--sftp-pass", SECRETS["rclone_password"]],
        torrent_client=f"qbittorrent://user:{SECRETS['client_password']}@127.0.0.1:8080",
    )
    monkeypatch.setattr(cfg, "seedbox", [seedbox])
    monkeypatch.setattr(base, "_learned_secrets", {SECRETS["authkey"], SECRETS["passkey"]})


def _all_secrets() -> str:
    return " ".join(SECRETS.values())


def test_the_filter_knows_every_secret_the_config_holds(planted: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.metadata.tidal, "client_secret", "tidal-client-secret")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "qobuz-user-token")
    monkeypatch.setattr(cfg.metadata.beatport, "password", "beatport-password")
    monkeypatch.setattr(cfg.upload.ai_review, "api_key", "ai-review-key")
    monkeypatch.setattr(cfg.proxy, "url", "socks5://user:proxy%2Fpass@127.0.0.1:1080")
    monkeypatch.setattr(cfg.proxy.services, "red", "http://red:red-proxy-pass@127.0.0.1:3128")
    monkeypatch.setattr(cfg.web, "token", "a-configured-web-token-0123456789abcdef")
    monkeypatch.setattr(cfg.tracker.red, "session", "abc/def+gh%3D")  # pyright: ignore[reportOptionalMemberAccess]
    found = set(egress.config_secrets(cfg))
    expected = {
        # The cookie as configured, decoded and as sent.
        "abc/def+gh%3D",
        "abc/def+gh=",
        "abc%2Fdef%2Bgh%3D",
        SECRETS["api_key"],
        SECRETS["image_key"],
        SECRETS["client_password"],
        SECRETS["rclone_password"],
        SECRETS["discogs"],
        "tidal-client-secret",
        "qobuz-user-token",
        "beatport-password",
        "ai-review-key",
        "proxy%2Fpass",
        "proxy/pass",
        "red-proxy-pass",
        "a-configured-web-token-0123456789abcdef",
    }
    assert expected <= found
    assert "" not in found


def test_no_secret_leaves_the_server_in_any_event(kind: Callable[[str, Run], None], planted: None) -> None:
    text = _all_secrets()

    async def done(_params: Params) -> dict[str, Any]:
        click.echo(f"out {text}")
        click.echo(f"err {text}", err=True)
        click.echo(f"https://tracker.test/torrents.php?action=download&authkey=x&torrent_pass={SECRETS['passkey']}")
        await interaction.prompt(f"prompt {text}", default=SECRETS["api_key"], type=click.Choice([SECRETS["session"]]))
        await interaction.confirm(f"confirm {text}")
        await interaction.edit(f"edit {text}")
        return {"result": text, SECRETS["token"]: [text]}

    async def fails(_params: Params) -> None:
        raise RuntimeError(f"failed {text}")

    async def unknown(_params: Params) -> None:
        raise UnknownOutcomeError(f"unknown {text}")

    kind("done", done)
    kind("fails", fails)
    kind("unknown", unknown)

    async def test(client: TestClient, manager: JobManager) -> None:
        events: list[Any] = []
        async with client.ws_connect("/api/ws", headers=AUTH) as socket:
            ids = [
                await _start(client, "done", name=text),
                await _start(client, "fails", folder=f"/music/{SECRETS['passkey']}"),
                await _start(client, "unknown"),
            ]
            await _answer(client, manager, ids[0], SECRETS["session"])
            await _answer(client, manager, ids[0], True)
            await _answer(client, manager, ids[0], "edited")
            for job_id in ids:
                await _finished(manager, job_id)
            while len([event for event in events if event["event"] == "finished"]) < 3:
                message = await socket.receive(timeout=5)
                assert message.type == WSMsgType.TEXT
                events.append(json.loads(message.data))
            sent = [json.dumps(events)]
            sent.append(await (await client.get("/api/jobs", headers=AUTH)).text())
            for job_id in ids:
                sent.append(await (await client.get(f"/api/jobs/{job_id}", headers=AUTH)).text())
        kinds = {event["event"] for event in events}
        assert kinds == {"created", "status", "log", "question", "answered", "finished"}
        everything = "\n".join(sent)
        assert "[REDACTED]" in everything
        for name, secret in SECRETS.items():
            assert secret not in everything, name
        statuses = {manager.jobs[job_id].status for job_id in ids}
        assert statuses == {"done", "failed", "unknown_outcome"}

    _with_app(test)


def test_an_authkey_learned_while_the_job_runs_is_masked_from_then_on(
    kind: Callable[[str, Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run(_params: Params) -> None:
        tracker = FakeTracker()
        api = _clients(await tracker.start(), "RED", unlimited=True)[0]

        async def index(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            return {"authkey": SECRETS["authkey"], "passkey": SECRETS["passkey"]}

        monkeypatch.setattr(api, "api_call", index)
        await api.authenticate()
        click.echo(f"announce https://tracker.test/{api.passkey}/x and {api.authkey}")
        await tracker.runner.cleanup()

    kind("learns", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "learns"))
        assert job.status == "done", job.error
        assert _log(job) == ["announce https://tracker.test/[REDACTED]/x and [REDACTED]"]

    _with_app(test)


# --- The websocket --------------------------------------------------------------------------


def test_the_websocket_needs_a_login_and_this_sites_origin() -> None:
    async def test(client: TestClient, _manager: JobManager) -> None:
        anonymous = await client.get("/api/ws", headers={"Connection": "Upgrade", "Upgrade": "websocket"})
        assert anonymous.status == 401
        foreign = await client.get(
            "/api/ws",
            headers={**AUTH, "Origin": "http://evil.test", "Connection": "Upgrade", "Upgrade": "websocket"},
        )
        assert foreign.status == 403
        own = f"http://{client.host}:{client.port}"
        async with client.ws_connect("/api/ws", headers=AUTH, origin=own) as socket:
            assert not socket.closed

    _with_app(test)


def test_a_browser_logged_in_with_its_cookie_gets_the_websocket() -> None:
    async def test(client: TestClient, _manager: JobManager) -> None:
        login = await client.post("/api/login", json={"token": TOKEN})
        assert login.status == 200
        own = f"http://{client.host}:{client.port}"
        async with client.ws_connect("/api/ws", origin=own) as socket:
            assert not socket.closed

    _with_app(test)


def test_the_websocket_streams_a_jobs_events(kind: Callable[[str, Run], None]) -> None:
    async def run(_params: Params) -> bool:
        click.echo("hello")
        return await interaction.confirm("Go?")

    kind("streams", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        async with client.ws_connect("/api/ws", headers=AUTH) as socket:
            job_id = await _start(client, "streams")
            await _answer(client, manager, job_id, True)
            events = []
            while not events or events[-1]["event"] != "finished":
                events.append(json.loads((await socket.receive(timeout=5)).data))
        assert [event["event"] for event in events] == ["created", "status", "log", "question", "answered", "finished"]
        assert events[2]["line"] == {"n": 1, "text": "hello", "err": False}
        assert events[-1]["job"]["result"] is True

    _with_app(test)


def test_a_connection_too_far_behind_is_cut_off(
    kind: Callable[[str, Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "SUBSCRIBER_BACKLOG", 3)

    async def test(_client: TestClient, manager: JobManager) -> None:
        subscriber = manager.subscribe()
        assert subscriber is not None
        for _ in range(5):
            manager._publish({"event": "test"})
        assert subscriber.cut_off
        events = [subscriber.events.get_nowait() for _ in range(subscriber.events.qsize())]
        assert events[-1] is None
        assert manager.subscribe() is not None

    _with_app(test)


# --- Tracker requests from jobs ---------------------------------------------------------------


@pytest.fixture
def short_period(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(account, "RATE_LIMIT_PERIOD", 1.0)
    monkeypatch.setattr(account, "RATE_LIMIT_MARGIN", 0.2)
    return 1.0


def test_jobs_share_one_budget_and_a_429_message_goes_to_the_job_that_got_it(
    kind: Callable[[str, Run], None], short_period: float
) -> None:
    tracker = FakeTracker()
    site: list[str] = []

    async def run(params: Params) -> None:
        (api,) = _clients(site[0], "RED")
        await _get(api, params.name)
        await asyncio.gather(*(_get(api) for _ in range(5)))
        await api.close()

    kind("requests", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        site.append(await tracker.start())
        tracker.limit_once["limited"] = "2"
        try:
            limited = await _start(client, "requests", name="limited")
            other = await _start(client, "requests", name="other")
            for job_id in (limited, other):
                assert (await _finished(manager, job_id)).status == "done"
            assert _log(manager.jobs[limited]) == ["Rate limit exceeded, waiting 2 seconds..."]
            assert _log(manager.jobs[other]) == []
        finally:
            await tracker.runner.cleanup()

    _with_app(test)
    # Twelve requests, and the one answered 429 sent again once the wait was over.
    assert len(tracker.hits) == 13
    # Every request after the 429, of both jobs, within one budget.
    after = sorted(tracker.times())[1:]
    assert _most_within(after, short_period) <= account.RATE_LIMIT_REQUESTS


def test_a_dry_run_job_sends_no_post(kind: Callable[[str, Run], None]) -> None:
    tracker = FakeTracker()
    site: list[str] = []

    async def run(_params: Params) -> None:
        (api,) = _clients(site[0], "RED", unlimited=True)
        try:
            await _get(api)
            await _post(api)
        finally:
            await api.close()

    kind("posts", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        site.append(await tracker.start())
        try:
            job = await _finished(manager, await _start(client, "posts", dry_run=True))
            assert job.status == "failed"
            assert job.error is not None
            assert "Dry run stopped before it could send POST" in job.error
        finally:
            await tracker.runner.cleanup()

    _with_app(test)
    assert [method for _, _, method, _ in tracker.hits] == ["GET"]


def test_cancelling_a_job_during_its_post_stops_it_once_the_tracker_answered(kind: Callable[[str, Run], None]) -> None:
    tracker = FakeTracker()
    site: list[str] = []
    went_on = threading.Event()

    async def run(_params: Params) -> None:
        (api,) = _clients(site[0], "RED", unlimited=True)
        try:
            await _post(api)
            went_on.set()
        finally:
            await api.close()

    kind("posts", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        site.append(await tracker.start())
        try:
            job_id = await _start(client, "posts")
            await _until(lambda: bool(tracker.hits))
            await client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=AUTH)
            job = await _finished(manager, job_id)
            assert job.status == "cancelled"
            assert tracker.posts == 0
            assert not went_on.is_set()
            assert any(line.startswith("Cancelled once POST /ajax.php?action=upload to RED") for line in _log(job))
        finally:
            await tracker.runner.cleanup()

    _with_app(test)
    assert [method for _, _, method, _ in tracker.hits] == ["POST"]


def test_jobs_requests_carry_their_output_to_their_own_log(
    kind: Callable[[str, Run], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The debug lines of each request are printed on the request loop, into the log of the job that sent it.
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", True)
    tracker = FakeTracker()
    site: list[str] = []

    async def run(params: Params) -> None:
        (api,) = _clients(site[0], "RED", unlimited=True)
        await _get(api, params.name)
        await api.close()

    kind("debug", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        site.append(await tracker.start())
        try:
            ids = {name: await _start(client, "debug", name=name) for name in ("first", "second")}
            for name, job_id in ids.items():
                job = await _finished(manager, job_id)
                params = [line for line in _log(job) if line.startswith("[DEBUG] params")]
                assert params == [f'[DEBUG] params: {{"action":"{name}"}}']
        finally:
            await tracker.runner.cleanup()

    _with_app(test)


def test_job_threads_do_not_outlive_the_server(kind: Callable[[str, Run], None]) -> None:
    kind("quick", lambda _params: asyncio.sleep(0))

    async def test(client: TestClient, manager: JobManager) -> None:
        await _finished(manager, await _start(client, "quick"))

    _with_app(test)
    deadline = time.monotonic() + 5
    while [thread for thread in threading.enumerate() if thread.name.startswith("salmon web job-")]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
