"""A salmon web job reads the tracker credentials from config.toml when it starts (#1034, ADR 0008).

A change saved while the server runs reaches the jobs started after it; a running job keeps what it started with.
Each test edits a copy of the config in tmp_path, never the one the run loaded. Trackers are local fakes on
127.0.0.1, served on the server's loop.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from aiohttp.test_utils import TestClient, TestServer
from helpers.checkconf_connection import FakeTracker, point_at
from helpers.webui_jobs import AUTH, TOKEN, Params, _finished, _log, _start, _until
from test_webui_jobs import kind  # noqa: F401

import salmon.config
import salmon.trackers
from salmon.config.edit import CredentialsUpdate, update_credentials
from salmon.errors import LoginError
from salmon.trackers import base, credentials
from salmon.webui import server
from salmon.webui.jobs import LOGIN_HINT, Job, JobManager

OLD_KEY = "old-api-key-0123456789"
RED_TABLE = "\n[tracker.red]\nsession = 'test-session-red'\n"


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the loaded config, RED with an API key, and RED the only tracker checked."""
    assert salmon.config.loaded_config_path is not None
    text = salmon.config.loaded_config_path.read_text(encoding="utf-8")
    assert text.count(RED_TABLE) == 1
    path = tmp_path / "config.toml"
    path.write_text(text.replace(RED_TABLE, f"{RED_TABLE}api_key = '{OLD_KEY}'\n"), encoding="utf-8")
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    return path


def _web(config: Path, fake: FakeTracker, test: Callable[[TestClient, JobManager], Awaitable[None]]) -> None:
    async def main() -> None:
        app = server.create_app(TOKEN, "127.0.0.1", [], config_path=config)
        async with fake.serving(), TestClient(TestServer(app, host="127.0.0.1")) as client:
            await test(client, app[server.JOBS])

    anyio.run(main)


async def _check(client: TestClient, manager: JobManager) -> Job:
    response = await client.post(
        "/api/jobs", json={"kind": "connection_check", "params": {"trackers": ["RED"]}}, headers=AUTH
    )
    assert response.status == 201, await response.text()
    return await _finished(manager, (await response.json())["id"])


def test_running_job_keeps_its_credentials(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTracker(hold=asyncio.Event())
    point_at(monkeypatch, {"RED": fake})

    async def test(client: TestClient, manager: JobManager) -> None:
        assert fake.hold is not None
        response = await client.post(
            "/api/jobs", json={"kind": "connection_check", "params": {"trackers": ["RED"]}}, headers=AUTH
        )
        job_id = (await response.json())["id"]
        await _until(lambda: len(fake.sent) == 1)
        update_credentials(config, "RED", CredentialsUpdate(session="new-cookie-123", api_key="new-key-456"))
        fake.hold.set()
        first = await _finished(manager, job_id)
        assert first.status == "done", first.error

        second = await _check(client, manager)
        assert second.status == "done", second.error

    _web(config, fake, test)

    assert fake.credentials == ["test-session-red", OLD_KEY, "new-cookie-123", "new-key-456"]


def test_invalid_file_fails_job_before_sending(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTracker()
    point_at(monkeypatch, {"RED": fake})
    config.write_text("[tracker", encoding="utf-8")

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _check(client, manager)
        assert job.status == "failed"
        assert job.error is not None and str(config) in job.error

    _web(config, fake, test)

    assert fake.sent == []


def test_new_cookie_is_masked_in_job_log(config: Path, kind: Callable[[str, Any], None]) -> None:  # noqa: F811
    """The job masks the cookie its snapshot read, wherever it is printed: the tracker client masks only its own."""
    planted = "planted-new-cookie-9f3a"

    async def run(_params: Params) -> None:
        cookie = credentials.credentials("RED")
        assert cookie is not None
        click.echo(f"cookie {cookie.session} printed")

    kind("leaky", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        update_credentials(config, "RED", CredentialsUpdate(session=planted))
        job = await _finished(manager, await _start(client, "leaky"))
        assert job.status == "done", job.error
        shown = "\n".join(_log(job))
        assert "printed" in shown
        assert planted not in shown

    _web(config, FakeTracker(), test)


def test_login_error_points_to_settings(config: Path, kind: Callable[[str, Any], None]) -> None:  # noqa: F811
    async def run(_params: Params) -> None:
        raise LoginError("RED redirected to its login page")

    kind("logged_out", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "logged_out"))
        assert job.status == "failed"
        assert job.error is not None and LOGIN_HINT in job.error
        # A refused API key raises LoginError too (BaseGazelleApi._send): the hint names both.
        assert "session cookie or API key" in job.error

    _web(config, FakeTracker(), test)


def test_missing_folder_does_not_fail_jobs(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A download folder or library that is offline fails no job: a job's start reads the trackers only."""
    offline = "download_directory = '/nonexistent/salmon-music'"
    config.write_text(re.sub(r"(?m)^download_directory = .*$", offline, config.read_text()), encoding="utf-8")
    fake = FakeTracker()
    point_at(monkeypatch, {"RED": fake})

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _check(client, manager)
        assert job.status == "done", job.error

    _web(config, fake, test)

    assert fake.credentials == ["test-session-red", OLD_KEY]
