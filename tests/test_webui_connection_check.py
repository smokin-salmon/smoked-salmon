"""salmon web's connection check: what ``salmon checkconf -t`` sends, only when asked, one run at a time (#652).

Every tracker is a local fake on 127.0.0.1, served on the test's loop, which is salmon web's request loop: nothing
leaves the machine (the network guard in conftest.py stands).
"""

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient
from test_checkconf_connection import (  # pyright: ignore[reportMissingImports]
    FakeTracker,
    Sent,
    point_at,
    red,  # noqa: F401
    run_checkconf,
)
from test_trackers_tls import (  # noqa: F401  # pyright: ignore[reportMissingImports]
    Certificates,
    Server,
    certificates,
)
from test_webui_jobs import (  # noqa: F401  # pyright: ignore[reportMissingImports]
    AUTH,
    SECRETS,
    _finished,
    _log,
    _until,
    _with_app,
    planted,
)

import salmon.trackers
from salmon import cfg
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi, request_dumps
from salmon.webui.jobs import Job, JobManager
from salmon.webui.kinds import ConnectionCheckParams

KEY = "an-api-key"
REQUESTS_WITH_KEY: list[Sent] = [("index", "cookie"), ("index", "api key")]


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED and OPS in the config, each with an API key."""
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    for tracker in (cfg.tracker.red, cfg.tracker.ops):
        assert tracker is not None
        monkeypatch.setattr(tracker, "api_key", KEY)


def _web(fakes: dict[str, FakeTracker], test: Callable[[TestClient, JobManager], Awaitable[None]]) -> None:
    """Run `test` against salmon web, with the fake trackers serving on the server's loop."""

    async def serving(client: TestClient, manager: JobManager) -> None:
        async with AsyncExitStack() as stack:
            for fake in fakes.values():
                await stack.enter_async_context(fake.serving())
            await test(client, manager)

    _with_app(serving)


async def _post(client: TestClient, *trackers: str) -> Any:
    return await client.post(
        "/api/jobs", json={"kind": "connection_check", "params": {"trackers": list(trackers)}}, headers=AUTH
    )


async def _check(client: TestClient, manager: JobManager, *trackers: str) -> Job:
    response = await _post(client, *trackers)
    assert response.status == 201, await response.text()
    job = await _finished(manager, (await response.json())["id"])
    assert job.status == "done", (job.error, _log(job))
    return job


def _found(job: Job) -> dict[str, dict[str, Any]]:
    return {each["tracker"]: each for each in job.result["trackers"]}


# --- The same requests as salmon checkconf -t -------------------------------------------------------


def test_a_job_sends_what_checkconf_sends(red: Callable[[str | None], None], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    red(KEY)
    by_cli = FakeTracker()
    point_at(monkeypatch, {"RED": by_cli})
    assert run_checkconf(by_cli, "-t", "RED").exit_code == 0

    by_web = FakeTracker()
    point_at(monkeypatch, {"RED": by_web})

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _check(client, manager, "red")
        assert job.params == ConnectionCheckParams(["RED"])
        assert job.title == "Connection check: RED"
        (found,) = job.result["trackers"]
        assert found["tracker"] == "RED"
        assert (found["ok"], found["cookie"], found["key"]) == (True, "ok", "ok")
        assert found["cookie_error"] is found["key_error"] is found["tls_error"] is None
        assert found["checked_at"].endswith("+00:00")
        # The result lines of the command, without the request dumps.
        assert _log(job) == [
            "",
            "[ Testing Tracker: RED ]",
            "",
            "[ Testing Session Cookie ]",
            "  ✔ Session cookie OK",
            "  ✔ API authentication OK",
            "",
            "✔ Successfully checked RED",
        ]

    _web({"RED": by_web}, test)
    assert by_cli.sent == by_web.sent == REQUESTS_WITH_KEY


def test_without_an_api_key_only_the_cookie_is_checked(
    red: Callable[[str | None], None],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    red(None)
    by_cli = FakeTracker()
    point_at(monkeypatch, {"RED": by_cli})
    assert run_checkconf(by_cli, "-t", "RED").exit_code == 0

    by_web = FakeTracker()
    point_at(monkeypatch, {"RED": by_web})

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _check(client, manager, "RED")
        (found,) = job.result["trackers"]
        assert (found["ok"], found["cookie"], found["key"]) == (True, "ok", "not_set")

    _web({"RED": by_web}, test)
    assert by_cli.sent == by_web.sent == [("index", "cookie")]


def test_a_refused_cookie_and_a_refused_key_are_each_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTracker(cookie_ok=False, key_ok=False)
    point_at(monkeypatch, {"RED": fake})

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _check(client, manager, "RED")
        (found,) = job.result["trackers"]
        assert (found["ok"], found["cookie"], found["key"]) == (False, "failed", "failed")
        assert found["cookie_error"] == found["key_error"] == '"bad credentials"'
        assert "✖ Error testing RED (session cookie, API key)" in _log(job)

    _web({"RED": fake}, test)
    assert fake.sent == REQUESTS_WITH_KEY


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_a_certificate_failure_sends_no_second_request(
    certificates: Certificates,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted: list[int] = []

    async def test(client: TestClient, manager: JobManager) -> None:
        server = await Server().serve_tls(certificates.self_signed)
        try:

            def tracker(_code: str) -> Callable[[], BaseGazelleApi]:
                def make() -> BaseGazelleApi:
                    site = salmon.trackers.tracker_classes["RED"]()
                    site.base_url = server.url
                    return site

                return make

            monkeypatch.setattr(salmon.trackers, "get_class", tracker)
            job = await _check(client, manager, "RED")
        finally:
            accepted.append(server.accepted)
            await server.close()
        (found,) = job.result["trackers"]
        assert found["ok"] is False
        assert found["tls_error"] == "TLS certificate verification failed for 127.0.0.1: self-signed certificate"
        assert (found["cookie"], found["key"]) == ("not_checked", "not_checked")
        assert "✖ Error testing RED (TLS certificate)" in _log(job)
        # The command's hint reads the server's environment: it stays with the command.
        assert not [line for line in _log(job) if "CA certificates" in line]

    _with_app(test)
    # The API key check would go to the same host: it is not sent.
    assert accepted == [1]


# --- Only when asked ------------------------------------------------------------------------------


def test_loading_the_page_and_the_dashboard_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = {"RED": FakeTracker(), "OPS": FakeTracker()}
    made: list[str] = []

    def refuse(code: str) -> None:
        made.append(code)
        raise AssertionError("a page load made a tracker client")

    monkeypatch.setattr(salmon.trackers, "get_class", refuse)

    async def test(client: TestClient, manager: JobManager) -> None:
        # The page, and every GET the dashboard makes: its overview, the jobs, and the events.
        for path in ("/", "/index.html", "/api/dashboard", "/api/jobs", "/api/upload/options", "/api/browse"):
            response = await client.get(path, headers=AUTH)
            assert response.status == 200, path
            await response.read()
        async with client.ws_connect("/api/ws", headers=AUTH) as socket:
            await socket.close()
        # And once more, as a reload does.
        assert (await client.get("/api/dashboard", headers=AUTH)).status == 200
        assert manager.jobs == {}

    _web(fakes, test)
    assert made == []
    assert fakes["RED"].sent == fakes["OPS"].sent == []


def test_a_second_start_while_one_runs_is_refused_with_the_running_jobs_id(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()
    fake = FakeTracker(hold=release)
    point_at(monkeypatch, {"RED": fake, "OPS": FakeTracker()})

    async def test(client: TestClient, manager: JobManager) -> None:
        first = await _post(client, "RED")
        assert first.status == 201
        first_id = (await first.json())["id"]
        await _until(lambda: bool(fake.sent))
        for trackers in (("RED",), ("OPS",), ()):
            second = await _post(client, *trackers)
            assert second.status == 409
            answer = await second.json()
            assert answer["job_id"] == first_id
            assert first_id in answer["detail"]
        assert list(manager.jobs) == [first_id]
        release.set()
        assert (await _finished(manager, first_id)).status == "done"
        # Once it ended, the next one starts.
        again = await _post(client, "RED")
        assert again.status == 201
        assert (await _finished(manager, (await again.json())["id"])).status == "done"

    _web({"RED": fake, "OPS": FakeTracker()}, test)
    # Two runs of two requests, and nothing from the refused starts.
    assert fake.sent == REQUESTS_WITH_KEY * 2


# --- Which trackers ---------------------------------------------------------------------------------


def test_every_configured_tracker_is_checked_once_in_order_and_a_429_is_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = {"RED": FakeTracker(rate_limited=1), "OPS": FakeTracker()}
    point_at(monkeypatch, fakes)
    started = 0.0

    async def test(client: TestClient, manager: JobManager) -> None:
        nonlocal started
        started = asyncio.get_running_loop().time()
        job = await _check(client, manager)
        assert job.params == ConnectionCheckParams(["RED", "OPS"])
        assert job.title == "Connection check: RED, OPS"
        assert list(_found(job)) == ["RED", "OPS"]
        assert all(each["ok"] for each in job.result["trackers"])

    _web(fakes, test)
    # RED's first request was answered 429, and sent again once the wait was over; OPS followed RED.
    assert fakes["RED"].sent == [("index", "cookie"), ("index", "cookie"), ("index", "api key")]
    assert fakes["OPS"].sent == REQUESTS_WITH_KEY
    red_times, ops_times = fakes["RED"].times, fakes["OPS"].times
    assert red_times[1] - red_times[0] >= 0.9
    assert max(red_times) <= min(ops_times)


def test_the_trackers_named_are_the_only_ones_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = {"RED": FakeTracker(), "OPS": FakeTracker()}
    point_at(monkeypatch, fakes)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _check(client, manager, "ops", "OPS")
        assert job.params == ConnectionCheckParams(["OPS"])

    _web(fakes, test)
    assert fakes["RED"].sent == []
    assert fakes["OPS"].sent == REQUESTS_WITH_KEY


@pytest.mark.parametrize("trackers", [["NOPE"], ["RED", "DIC"], ["RED,NOPE"]])
def test_a_tracker_not_in_the_config_is_refused_before_any_job(
    monkeypatch: pytest.MonkeyPatch, trackers: list[str]
) -> None:
    fakes = {"RED": FakeTracker(), "OPS": FakeTracker()}
    point_at(monkeypatch, fakes)

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post(client, *trackers)
        assert response.status == 422
        bad = "DIC" if "DIC" in trackers else "NOPE"
        assert (await response.json())["detail"] == f"{bad} is not a tracker in your config (RED, OPS)."
        assert manager.jobs == {}

    _web(fakes, test)
    assert fakes["RED"].sent == fakes["OPS"].sent == []


def test_with_no_tracker_in_the_config_there_is_nothing_to_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(salmon.trackers, "tracker_list", [])

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post(client)
        assert response.status == 422
        assert (await response.json())["detail"] == "No tracker is configured."
        assert manager.jobs == {}

    _web({}, test)


def test_the_job_routes_for_it_need_a_login() -> None:
    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await client.post("/api/jobs", json={"kind": "connection_check", "params": {}})
        assert response.status == 401

    _web({}, test)


# --- No request dumps, no config write ---------------------------------------------------------------


def test_a_job_shows_no_request_dumps_beside_a_run_that_asks_for_them(
    red: Callable[[str | None], None],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    red(KEY)
    by_cli = FakeTracker()
    point_at(monkeypatch, {"RED": by_cli})
    # The command prints the dumps: it used to leave them on for the rest of the process.
    assert "[DEBUG] GET" in run_checkconf(by_cli, "-t", "RED").output
    assert cfg.upload.debug_tracker_connection is False

    by_web = FakeTracker()
    point_at(monkeypatch, {"RED": by_web})

    async def test(client: TestClient, manager: JobManager) -> None:
        # A run with the dumps on, in this context, while the job runs.
        with request_dumps():
            job = await _check(client, manager, "RED")
        assert not [line for line in _log(job) if line.startswith("[DEBUG]")]
        assert len(_log(job)) == 8
        assert cfg.upload.debug_tracker_connection is False

    _web({"RED": by_web}, test)
    assert by_web.sent == REQUESTS_WITH_KEY


# --- Secrets --------------------------------------------------------------------------------------------


def test_no_secret_is_in_any_event_or_the_result(planted: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    text = " ".join(SECRETS.values())
    # The tracker refuses both, and says why with every secret in it, as an error page may.
    fakes = {"RED": FakeTracker(cookie_ok=False, key_ok=False, error=text)}
    point_at(monkeypatch, fakes)

    async def test(client: TestClient, manager: JobManager) -> None:
        events: list[Any] = []
        async with client.ws_connect("/api/ws", headers=AUTH) as socket:
            response = await _post(client, "RED")
            assert response.status == 201
            job_id = (await response.json())["id"]
            await _finished(manager, job_id)
            while len([event for event in events if event["event"] == "finished"]) < 1:
                message = await socket.receive(timeout=5)
                assert message.type == WSMsgType.TEXT
                events.append(json.loads(message.data))
        job = manager.jobs[job_id]
        assert job.status == "done", (job.error, _log(job))
        (found,) = job.result["trackers"]
        assert found["cookie"] == found["key"] == "failed"
        sent = [json.dumps(events), json.dumps(job.result), "\n".join(_log(job))]
        sent.append(await (await client.get("/api/jobs", headers=AUTH)).text())
        sent.append(await (await client.get(f"/api/jobs/{job_id}", headers=AUTH)).text())
        everything = "\n".join(sent)
        assert "[REDACTED]" in everything
        for name, secret in SECRETS.items():
            assert secret not in everything, name

    _web(fakes, test)
    assert fakes["RED"].sent == REQUESTS_WITH_KEY
