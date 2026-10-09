"""salmon web's checks job against trackers: what ``salmon check all -t`` sends, through the request loop (#651).

Every tracker is a local fake on 127.0.0.1, served on the test's loop, which is salmon web's request loop: nothing
leaves the machine (the network guard in conftest.py stands). The albums are synthetic tagged FLACs, checked for
real; `flac` is a fake on PATH, as CI has none.
"""

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC
from tenacity import wait_none
from test_checks_all_command import _snapshot, fake_flac  # noqa: F401  # pyright: ignore[reportMissingImports]
from test_checks_mqa import _write_flac  # pyright: ignore[reportMissingImports]
from test_trackers_account import _most_within  # pyright: ignore[reportMissingImports]
from test_webui_jobs import AUTH, _finished, _log, _with_app  # pyright: ignore[reportMissingImports]

import salmon.checks.album as album_checks
import salmon.checks.do_not_upload as do_not_upload
import salmon.trackers
from salmon import cfg
from salmon.checks import all_checks
from salmon.config.validations import GazelleTrackerSettings
from salmon.trackers import account, base
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs
from salmon.webui.jobs import Job, JobManager
from salmon.webui.kinds import ChecksParams

# Two search strings for one artist: an index call and two browses per job.
ALBUM = "Album Vol. 2"
SEARCHSTRS = generate_dupe_check_searchstrs([("Artist", "main")], ALBUM, None)

Sent = tuple[str, str, dict[str, str]]


class FakeTracker:
    """A local Gazelle tracker answering index and browse, which records each request, when and on what connection.

    `answers` are sent, in turn, to the first browses instead of the usual answer.
    """

    def __init__(self, answers: list[Callable[[], web.Response]] | None = None, authkey: str = "authkey") -> None:
        self.answers = list(answers or [])
        self.authkey = authkey
        self.sent: list[Sent] = []
        self.times: list[float] = []
        self.ports: list[int] = []
        self.url = ""

    async def _handle(self, request: web.Request) -> web.Response:
        self.sent.append((request.method, request.path, dict(request.query)))
        self.times.append(time.monotonic())
        assert request.transport is not None
        self.ports.append(request.transport.get_extra_info("peername")[1])
        action = request.query.get("action")
        if action == "index":
            return web.json_response(
                {"status": "success", "response": {"authkey": self.authkey, "passkey": f"pass{self.authkey}"}}
            )
        if action == "browse":
            if self.answers:
                return self.answers.pop(0)()
            return web.json_response({"status": "success", "response": {"results": []}})
        return web.Response(status=404)

    @asynccontextmanager
    async def serving(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            yield self
        finally:
            await runner.cleanup()


def _expected(searchstrs: list[str] = SEARCHSTRS) -> list[Sent]:
    """What salmon up's dupe search sends one tracker: the index call, then one browse per search string."""
    return [
        ("GET", "/ajax.php", {"action": "index"}),
        *(("GET", "/ajax.php", {"action": "browse", "searchstr": s}) for s in searchstrs),
    ]


def _in_order(sent: list[Sent]) -> list[Sent]:
    """The requests, the browses sorted: they are sent together, so they may arrive in either order."""
    return sent[:1] + sorted(sent[1:], key=lambda each: each[2].get("searchstr", ""))


def _album(folder: Path, title: str = ALBUM) -> Path:
    folder.mkdir(parents=True)
    for number in (1, 2):
        track = folder / f"{number:02d} Track {number}.flac"
        _write_flac(track, mqa_marker=False)
        tags = FLAC(track)
        tags.update({"artist": "Artist", "album": title, "date": "2020", "title": f"Track {number}"})
        tags["tracknumber"] = str(number)
        tags.save()
    return folder


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the authkeys and passkeys other tests' fake trackers gave, short ones among them."""
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without the backoff wait; a 429 still waits the 2 s it asks for."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """RED and OPS in the config, and Do-Not-Upload lists that list nothing, so each named tracker is searched."""
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "red.toml").write_text("")
    (lists / "ops.toml").write_text("")
    monkeypatch.setattr(do_not_upload, "LISTS_DIR", lists)


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    folder, library = tmp_path / "downloads", tmp_path / "library"
    folder.mkdir()
    library.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(folder))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    return folder


def _point_at(monkeypatch: pytest.MonkeyPatch, fakes: dict[str, FakeTracker]) -> list[BaseGazelleApi]:
    """Make every tracker client a client of the fake for its site code, with the account's own rate limit."""
    built: list[BaseGazelleApi] = []

    def client(code: str) -> BaseGazelleApi:
        site = salmon.trackers.tracker_classes[code]()
        site.base_url = fakes[code].url
        built.append(site)
        return site

    monkeypatch.setattr(salmon.trackers, "get_class", lambda code: partial(client, code))
    return built


def _web(fakes: dict[str, FakeTracker], test: Callable[[TestClient, JobManager], Awaitable[None]]) -> None:
    """Run `test` against salmon web, with the fake trackers serving on the server's loop."""

    async def serving(client: TestClient, manager: JobManager) -> None:
        async with AsyncExitStack() as stack:
            for fake in fakes.values():
                await stack.enter_async_context(fake.serving())
            await test(client, manager)

    _with_app(serving)


def _cli(fakes: dict[str, FakeTracker], *args: str) -> Any:
    """Run ``salmon check all`` with the fake trackers serving."""

    async def run() -> Any:
        async with AsyncExitStack() as stack:
            for fake in fakes.values():
                await stack.enter_async_context(fake.serving())
            return await CliRunner().invoke(all_checks, list(args))

    return anyio.run(run)


async def _post_checks(client: TestClient, path: Path, trackers: list[str], **params: Any) -> Any:
    body = {"kind": "checks", "params": {"path": str(path), "trackers": trackers, **params}}
    return await client.post("/api/jobs", json=body, headers=AUTH)


async def _run_checks(client: TestClient, manager: JobManager, path: Path, trackers: list[str]) -> Job:
    response = await _post_checks(client, path, trackers)
    assert response.status == 201, await response.text()
    job = await _finished(manager, (await response.json())["id"])
    assert job.status == "done", (job.error, _log(job))
    return job


def _rows(job: Job) -> dict[str, dict[str, Any]]:
    return {row["check"]: row for row in job.result["rows"]}


# --- The same requests as salmon check all -t -----------------------------------------------------


@pytest.mark.parametrize("trackers", [["RED"], ["RED", "OPS"]])
def test_a_checks_job_sends_what_check_all_sends(
    monkeypatch: pytest.MonkeyPatch, downloads: Path, trackers: list[str]
) -> None:
    album = _album(downloads / "Artist - Album (2020) [WEB FLAC]")
    before = _snapshot(album)

    by_cli = {code: FakeTracker() for code in ("RED", "OPS")}
    _point_at(monkeypatch, by_cli)
    result = _cli(by_cli, str(album), *(arg for code in trackers for arg in ("-t", code)))
    assert result.exit_code == 0, result.output

    by_web = {code: FakeTracker() for code in ("RED", "OPS")}
    _point_at(monkeypatch, by_web)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _run_checks(client, manager, album, [code.lower() for code in trackers])
        assert job.params == ChecksParams(str(album), trackers=trackers)
        assert job.title == f"Checks against {', '.join(trackers)}: {album.name}"
        assert job.result["trackers"] == trackers
        rows = _rows(job)
        for code in trackers:
            assert rows[f"Dupe ({code})"]["verdict"] == "OK"
            assert rows[f"Dupe ({code})"]["detail"] == f"No group found on {code}."
        assert "Dupe (OPS)" in rows if "OPS" in trackers else "Dupe (OPS)" not in rows

    _web(by_web, test)
    for code in ("RED", "OPS"):
        expected = _expected() if code in trackers else []
        assert _in_order(by_cli[code].sent) == expected, code
        assert _in_order(by_web[code].sent) == expected, code
    assert _snapshot(album) == before


def test_without_a_tracker_nothing_is_contacted_and_no_client_is_made(
    monkeypatch: pytest.MonkeyPatch, downloads: Path
) -> None:
    album = _album(downloads / "Album")
    fakes = {"RED": FakeTracker(), "OPS": FakeTracker()}

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a checks job without a tracker made a tracker client")

    monkeypatch.setattr(salmon.trackers, "get_class", refuse)
    monkeypatch.setattr(BaseGazelleApi, "__init__", refuse)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _run_checks(client, manager, album, [])
        assert job.title == f"Checks: {album.name}"
        rows = _rows(job)
        assert not [check for check in rows if check.startswith("Dupe")]
        # RED's and OPS's rules apply, as check all's without -t.
        assert {"Do-Not-Upload (RED)", "Do-Not-Upload (OPS)"} <= set(rows)

    _web(fakes, test)
    assert fakes["RED"].sent == fakes["OPS"].sent == []


@pytest.mark.parametrize("trackers", [["NOPE"], ["RED", "DIC"], ["RED,NOPE"]])
def test_a_tracker_not_in_the_config_is_refused_before_any_job(
    monkeypatch: pytest.MonkeyPatch, downloads: Path, trackers: list[str]
) -> None:
    album = _album(downloads / "Album")
    fakes = {"RED": FakeTracker(), "OPS": FakeTracker()}
    built = _point_at(monkeypatch, fakes)

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post_checks(client, album, trackers)
        assert response.status == 422
        bad = "DIC" if "DIC" in trackers else "NOPE"
        assert (await response.json())["detail"] == f"{bad} is not a tracker in your config (RED, OPS)."
        assert manager.jobs == {}

    _web(fakes, test)
    assert built == []
    assert fakes["RED"].sent == fakes["OPS"].sent == []


# --- Through the request loop ---------------------------------------------------------------------


def test_two_checks_jobs_on_one_account_share_its_budget(monkeypatch: pytest.MonkeyPatch, downloads: Path) -> None:
    # A one second period, with a margin as large for it as the real one is for 10 s: only the times change.
    monkeypatch.setattr(account, "RATE_LIMIT_PERIOD", 1.0)
    monkeypatch.setattr(account, "RATE_LIMIT_MARGIN", 0.2)
    first, second = _album(downloads / "First"), _album(downloads / "Second")
    fakes = {"RED": FakeTracker()}
    _point_at(monkeypatch, fakes)

    # Both jobs search at once: each has done its file checks before either sends anything.
    together = threading.Barrier(2)
    dupe_row = album_checks._dupe_row

    async def at_once(tracker: str, release: dict[str, Any]) -> Any:
        await anyio.to_thread.run_sync(together.wait, 30)
        return await dupe_row(tracker, release)

    monkeypatch.setattr(album_checks, "_dupe_row", at_once)

    async def test(client: TestClient, manager: JobManager) -> None:
        responses = [await _post_checks(client, path, ["RED"]) for path in (first, second)]
        ids = [(await response.json())["id"] for response in responses]
        for job_id in ids:
            job = await _finished(manager, job_id)
            assert job.status == "done", (job.error, _log(job))

    _web(fakes, test)
    # Two jobs, each its index call and two browses: six requests, of which five enter in any period.
    assert len(fakes["RED"].sent) == 2 * len(_expected())
    assert _most_within(fakes["RED"].times, 1.0) == account.RATE_LIMIT_REQUESTS


def test_a_429_is_waited_for_as_in_the_cli(monkeypatch: pytest.MonkeyPatch, downloads: Path) -> None:
    album = _album(downloads / "Album")

    def too_many() -> web.Response:
        return web.json_response({"status": "failure"}, status=429, headers={"Retry-After": "1"})

    by_cli = {"RED": FakeTracker([too_many])}
    _point_at(monkeypatch, by_cli)
    result = _cli(by_cli, str(album), "-t", "RED")
    assert result.exit_code == 0, result.output

    by_web = {"RED": FakeTracker([too_many])}
    _point_at(monkeypatch, by_web)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _run_checks(client, manager, album, ["RED"])
        assert _rows(job)["Dupe (RED)"]["verdict"] == "OK"

    _web(by_web, test)
    for fake in (by_cli["RED"], by_web["RED"]):
        # The first browse to arrive got the 429: it alone is sent once more, after the 2 s salmon waits at least.
        limited = fake.sent[1]
        assert _in_order(fake.sent) == _in_order([*_expected(), limited])
        again = fake.sent.index(limited, 2)
        assert fake.times[again] - fake.times[1] >= 2 - 0.05
    assert _in_order(by_cli["RED"].sent) == _in_order(by_web["RED"].sent)


def test_an_error_answer_is_the_dupe_rows_and_the_other_rows_are_there(
    monkeypatch: pytest.MonkeyPatch, downloads: Path
) -> None:
    album = _album(downloads / "Album")

    def failure() -> web.Response:
        return web.json_response({"status": "failure", "error": "the search is down"})

    fakes = {"RED": FakeTracker([failure])}
    _point_at(monkeypatch, fakes)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _run_checks(client, manager, album, ["RED"])
        rows = _rows(job)
        assert rows["Dupe (RED)"]["verdict"] == "WARN"
        assert rows["Dupe (RED)"]["detail"] == "Could not search RED: the search is down"
        for check in ("Source", "Integrity", "MQA", "Tags", "Provenance", "Do-Not-Upload (RED)"):
            assert check in rows

    _web(fakes, test)
    # Not sent again: the tracker answered.
    assert _in_order(fakes["RED"].sent) == _expected()


def test_the_accounts_pool_outlives_a_job_and_serves_the_next(monkeypatch: pytest.MonkeyPatch, downloads: Path) -> None:
    # One search string: two requests per job, so the second job's do not wait for the rate limit's period.
    album = _album(downloads / "Album", title="Album")
    fakes = {"RED": FakeTracker()}
    built = _point_at(monkeypatch, fakes)

    async def test(client: TestClient, manager: JobManager) -> None:
        await _run_checks(client, manager, album, ["RED"])
        # The job closed its client: on salmon web's request loop the account keeps its pool for the next job.
        assert built[0]._session is None
        pool = account._accounts[asyncio.get_running_loop()]["RED"].pool
        assert pool is not None
        assert not pool.closed
        first = len(fakes["RED"].sent)

        await _run_checks(client, manager, album, ["RED"])
        assert len(built) == 2
        assert account._accounts[asyncio.get_running_loop()]["RED"].pool is pool
        assert not pool.closed
        # The second job's requests went out on the first job's connections: no new handshake.
        assert set(fakes["RED"].ports[first:]) <= set(fakes["RED"].ports[:first])
        pools.append(pool)

    pools: list[Any] = []
    _web(fakes, test)
    # Closed when salmon web stopped.
    assert pools[0].closed


def test_no_planted_secret_leaves_in_any_event_and_the_folder_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, downloads: Path
) -> None:
    planted = {
        "session": "planted-session-cookie-3c7e",
        "api_key": "planted-red-api-key-b812",
        "authkey": "plantedauthkey5a0f",
    }
    monkeypatch.setattr(
        cfg.tracker, "red", GazelleTrackerSettings(session=planted["session"], api_key=planted["api_key"])
    )
    album = _album(downloads / f"Album {planted['api_key']}")
    before = _snapshot(album)

    def echoing() -> web.Response:
        # A tracker error repeating what it was sent.
        return web.json_response(
            {"status": "failure", "error": f"bad key {planted['api_key']} for {planted['authkey']}"}
        )

    fakes = {"RED": FakeTracker([echoing], authkey=planted["authkey"])}
    _point_at(monkeypatch, fakes)

    async def test(client: TestClient, manager: JobManager) -> None:
        events: list[Any] = []
        async with client.ws_connect("/api/ws", headers=AUTH) as socket:
            response = await _post_checks(client, album, ["RED"], report=True)
            assert response.status == 201, await response.text()
            job_id = (await response.json())["id"]
            job = await _finished(manager, job_id)
            assert job.status == "done", (job.error, _log(job))
            while not any(event["event"] == "finished" for event in events):
                message = await socket.receive(timeout=5)
                assert message.type == WSMsgType.TEXT
                events.append(json.loads(message.data))
            sent = [json.dumps(events)]
            sent.append(await (await client.get("/api/jobs", headers=AUTH)).text())
            sent.append(await (await client.get(f"/api/jobs/{job_id}", headers=AUTH)).text())
        everything = "\n".join(sent)
        assert "Could not search RED: bad key" in everything
        assert "[REDACTED]" in everything
        for name, secret in {**planted, "passkey": f"pass{planted['authkey']}"}.items():
            assert secret not in everything, name

    _web(fakes, test)
    assert _snapshot(album) == before
