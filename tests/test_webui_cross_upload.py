"""salmon web's cross-upload job: the same run as salmon cross-upload, through the HTTP API (#658, ADR 0004).

Both trackers are local fakes on 127.0.0.1 serving on salmon web's request loop, and the image hosts and seedbox are
in-process fakes, those of tests/test_cross_upload.py: nothing leaves the machine (the network guard in conftest.py
stands). The OPS answers come from tests/fixtures/cross_upload (a made-up release).
"""

import asyncio
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import asyncclick as click
import msgspec
import pytest
import test_uploader_dry_run as up_world  # pyright: ignore[reportMissingImports]
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient
from tenacity import wait_none
from test_cross_upload import (  # pyright: ignore[reportMissingImports]
    API_KEYS,
    AUTHKEY,
    FOLDER,
    PASSKEY,
    SESSIONS,
    TARGET_GROUP_ID,
    TORRENT_ID,
    FakeTracker,
    Sent,
    World,
    _album,
    _client,
    _converted,
    _cross_upload,
    _ops_answer,
    _snapshot,
    _transcode,
    cross_upload_world,
    dirs,  # noqa: F401
    settings,  # noqa: F401
)
from test_trackers_account import _most_within  # pyright: ignore[reportMissingImports]
from test_webui_jobs import AUTH, _finished, _log, _until, _with_app  # pyright: ignore[reportMissingImports]
from test_webui_upload import PARAMS as UP_PARAMS  # pyright: ignore[reportMissingImports]
from test_webui_upload import _drive  # pyright: ignore[reportMissingImports]

import salmon.cross_upload as cross_upload_module
import salmon.trackers
import salmon.uploader
from salmon import cfg
from salmon.config.validations import GazelleTrackerSettings
from salmon.errors import CRCMismatchError
from salmon.trackers import account, base
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import staging
from salmon.webui import jobs
from salmon.webui.jobs import UNKNOWN_OUTCOME, JobKind, JobManager

pytestmark = pytest.mark.usefixtures("settings")

# A torrent whose row on the fixture group page says lossy master approved.
APPROVED_ID = 600011


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the authkeys and passkeys other tests' fake trackers gave, short ones among them."""
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without the backoff wait; a 429 still waits the 2 s it asks for."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


# --- Running a cross-upload job -----------------------------------------------------------------


Prepare = Callable[[FakeTracker, FakeTracker], None]


def _holding(album: Path, torrent_id: int = TORRENT_ID, change: Callable[[dict[str, Any]], None] | None = None):
    """prepare: SOURCE (OPS) holds the album's torrent; `change` alters its answer."""

    def prepare(source: FakeTracker, _target: FakeTracker) -> None:
        source.torrents[torrent_id] = _ops_answer(album, torrent_id)
        if change is not None:
            change(source.torrents[torrent_id])

    return prepare


def _web(
    world: World,
    test: Callable[[TestClient, JobManager], Awaitable[None]],
    prepare: Prepare | None = None,
    max_jobs: int = 2,
) -> None:
    """Run `test` against salmon web, with the fake trackers serving on the server's loop."""

    async def serving(client: TestClient, manager: JobManager) -> None:
        async with world.serving():
            if prepare is not None:
                prepare(world.trackers["OPS"], world.trackers["RED"])
            await test(client, manager)

    _with_app(serving, max_jobs=max_jobs)


async def _post(client: TestClient, inputs: list[str] | None = None, **body: Any) -> Any:
    params = {"inputs": inputs or [str(TORRENT_ID)], "source": "OPS", "target": "RED", **body.pop("params", {})}
    return await client.post("/api/jobs", json={"kind": "cross_upload", "params": params, **body}, headers=AUTH)


async def _start(client: TestClient, inputs: list[str] | None = None, **body: Any) -> str:
    response = await _post(client, inputs, **body)
    assert response.status == 201, await response.text()
    return (await response.json())["id"]


def _shown(sent: Sent, urls: dict[str, str]) -> tuple[Any, ...]:
    """A request as the fake got it, every fake's address replaced by its tracker's name: each run's fakes listen on
    other ports."""

    def value(field: Any) -> Any:
        if isinstance(field, str):
            for url, name in urls.items():
                field = field.replace(url, name)
        return field

    fields = {name: [value(each) for each in values] for name, values in sent.fields.items()}
    return (sent.method, sent.path, sent.query, sent.cookie, sent.authorization, fields, sent.files)


def _requests(source: FakeTracker, target: FakeTracker) -> dict[str, list[tuple[Any, ...]]]:
    """What each fake got, in order: every field and file."""
    urls = {source.url: f"<{source.code}>", target.url: f"<{target.code}>"}
    return {tracker.code: [_shown(sent, urls) for sent in tracker.sent] for tracker in (source, target)}


def _downloads_of_its_own(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """A download_directory under root, configured: a run of the command beside the job's leaves the job's alone."""
    downloads = root / "cli-downloads"
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    return downloads


def _target_uploads(tracker: FakeTracker) -> list[Sent]:
    return [sent for sent in tracker.posts() if sent.path in ("/ajax.php", "/upload.php")]


# --- The same requests as salmon cross-upload -------------------------------------------------------


@pytest.mark.parametrize(
    ("conversion", "formats"),
    [
        ([], [("FLAC", "Lossless")]),
        (["--transcode", "V0"], [("FLAC", "Lossless"), ("MP3", "V0 (VBR)")]),
        (["--downconvert"], [("FLAC", "24bit Lossless"), ("FLAC", "Lossless")]),
    ],
    ids=["flac", "v0", "16-bit"],
)
def test_a_web_cross_upload_sends_what_the_command_sends(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
    tmp_path: Path,
    conversion: list[str],
    formats: list[tuple[str, str]],
) -> None:
    downconvert = conversion == ["--downconvert"]
    monkeypatch.setattr(salmon.uploader, "convert_folder", _converted)

    def album(root: Path) -> Path:
        return _album(root / FOLDER, 96000, bits=24) if downconvert else _album(root / FOLDER)

    def change(answer: dict[str, Any]) -> None:
        if downconvert:
            answer["torrent"]["encoding"] = "24bit Lossless"

    # salmon cross-upload, every question answered with its default (an empty line).
    cli_album = album(_downloads_of_its_own(monkeypatch, tmp_path))
    cli = _cross_upload(
        monkeypatch,
        dirs,
        [str(TORRENT_ID), *conversion],
        input="\n" * 20,
        prepare=_holding(cli_album, change=change),
        transcode=_transcode,
    )
    assert cli.result.exit_code == 0, cli.output
    monkeypatch.setattr(cfg.directory, "download_directory", str(dirs.downloads))

    # The same release and answers, as a web job.
    web_album = album(dirs.downloads)
    world = cross_upload_world(monkeypatch, dirs, transcode=_transcode)
    params: dict[str, Any] = {"transcodes": ["V0"]} if conversion[:1] == ["--transcode"] else {}
    if downconvert:
        params["downconvert"] = True

    async def test(client: TestClient, manager: JobManager) -> None:
        job, asked = await _drive(client, manager, await _start(client, params=params))
        assert job.status == "done", (job.error, _log(job))
        # Each question the command asked in the terminal, in the same order: the plan, then the dupe check.
        shown = click.unstyle(cli.output)
        at = 0
        for question in asked:
            at = shown.index(click.unstyle(question["text"]).strip(), at)
        assert [(question["kind"], click.unstyle(question["text"]).split("\n")[1]) for question in asked] == [
            ("confirm", "Cross-upload this to RED?"),
            ("prompt", "Would you like to upload to an existing group?"),
        ]
        # Each torrent uploaded, linked.
        red = world.trackers["RED"].url
        assert job.result["uploads"] == [
            {"tracker": "RED", "format": format_, "url": f"{red}/torrents.php?torrentid={700001 + number}"}
            for number, (format_, _bitrate) in enumerate(formats)
        ]

    _web(world, test, _holding(web_album, change=change))

    assert _requests(world.trackers["OPS"], world.trackers["RED"]) == _requests(cli.source, cli.target)
    uploads = _target_uploads(world.trackers["RED"])
    assert [(sent.fields["format"][0], sent.fields["bitrate"][0]) for sent in uploads] == formats
    # The real forms: the torrent file in each.
    for sent in uploads:
        assert sent.files["file_input"][0].startswith(b"d")
    assert _snapshot(web_album) == _snapshot(cli_album)


def test_the_form_offers_the_trackers_and_transcodes_cross_upload_takes(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
) -> None:
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    world = cross_upload_world(monkeypatch, dirs)

    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await client.get("/api/cross-upload/options", headers=AUTH)
        assert response.status == 200
        assert await response.json() == {"trackers": ["RED", "OPS"], "transcodes": ["320", "V0"], "max_releases": 5}

    _web(world, test)
    assert world.trackers["OPS"].sent == world.trackers["RED"].sent == []


# --- Dry run --------------------------------------------------------------------------------------------


def test_a_dry_run_job_reads_what_the_commands_dry_run_reads_and_sends_no_post(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
    tmp_path: Path,
) -> None:
    cli_album = _album(_downloads_of_its_own(monkeypatch, tmp_path) / FOLDER)
    args = [str(TORRENT_ID), "--dry-run", "--transcode", "V0"]
    cli = _cross_upload(monkeypatch, dirs, args, input="\n" * 20, prepare=_holding(cli_album), transcode=_transcode)
    assert cli.result.exit_code == 0, cli.output
    monkeypatch.setattr(cfg.directory, "download_directory", str(dirs.downloads))

    album = _album(dirs.downloads / FOLDER)
    before = _snapshot(album)
    world = cross_upload_world(monkeypatch, dirs, transcode=_transcode)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, params={"transcodes": ["V0"]}, dry_run=True)
        job, _asked = await _drive(client, manager, job_id)
        assert job.status == "done", (job.error, _log(job))
        assert job.result["uploads"] == []
        log = _log(job)
        red = world.trackers["RED"].url
        would_send = f"Dry run: not uploading to RED. It would send POST {red}/ajax.php?action=upload with the API key:"
        # The FLAC's form and its V0 transcode's, part by part, and the torrent each holds.
        assert log.count(would_send) == 2
        assert log.count("  auth: [REDACTED]") == 2
        assert len([line for line in log if line.startswith("  file_input: ") and ".torrent (" in line]) == 2
        assert len([line for line in log if line.startswith("  The torrent: ")]) == 2
        assert log[-1] == "Dry run: done. Nothing was sent."

    _web(world, test, _holding(album))

    # SOURCE got the reads the command's dry run sends, TARGET its reads, and neither a POST.
    assert _requests(world.trackers["OPS"], world.trackers["RED"]) == _requests(cli.source, cli.target)
    assert world.trackers["OPS"].posts() == world.trackers["RED"].posts() == []
    assert world.images == world.seeded == world.copied == []
    # Nothing left behind: no torrent file, no transcode, no scratch directory, the album as it was.
    assert list(dirs.torrents.iterdir()) == []
    assert sorted(os.listdir(dirs.downloads)) == [staging.STAGING_DIR, FOLDER]
    assert os.listdir(dirs.downloads / staging.STAGING_DIR) == []
    assert _snapshot(album) == before


# --- TARGET's answers: a 429, a lost answer -------------------------------------------------------------


def test_a_429_on_the_target_upload_is_waited_for_and_sent_once_more(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
    no_backoff: None,
) -> None:
    album = _album(dirs.downloads / FOLDER)
    world = cross_upload_world(monkeypatch, dirs)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _holding(album)(source, target)
        target.uploads = ["429"]

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(client, manager, await _start(client, assume_defaults=True))
        assert job.status == "done", (job.error, _log(job))
        assert "Rate limit exceeded, waiting 2 seconds..." in _log(job)
        assert [each["url"] for each in job.result["uploads"]] == [
            f"{world.trackers['RED'].url}/torrents.php?torrentid=700001"
        ]

    _web(world, test, prepare)
    # The very same form twice: its fields and its torrent file.
    first, second = _target_uploads(world.trackers["RED"])
    assert (first.fields, first.files) == (second.fields, second.files)


def test_an_answer_lost_after_the_upload_ends_the_job_as_unknown_outcome_with_nothing_sent_again(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
    tmp_path: Path,
) -> None:
    def dropping(album: Path) -> Prepare:
        def prepare(source: FakeTracker, target: FakeTracker) -> None:
            _holding(album)(source, target)
            target.uploads = ["drop"]

        return prepare

    cli_album = _album(_downloads_of_its_own(monkeypatch, tmp_path) / FOLDER)
    cli = _cross_upload(monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", prepare=dropping(cli_album))
    assert cli.result.exit_code == 1, cli.output
    monkeypatch.setattr(cfg.directory, "download_directory", str(dirs.downloads))
    album = _album(dirs.downloads / FOLDER)
    world = cross_upload_world(monkeypatch, dirs)

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(client, manager, await _start(client, assume_defaults=True))
        assert job.status == "unknown_outcome", (job.error, _log(job))
        assert job.error is not None
        assert job.error.startswith(f"{UNKNOWN_OUTCOME} (")
        assert "The upload may still have gone through" in job.error
        assert job.result == {"source": "OPS", "target": "RED", "inputs": [str(TORRENT_ID)], "uploads": []}
        assert any(line.startswith("Stopping: ") for line in _log(job))

    _web(world, test, dropping(album))
    # One upload, looked up by its infohash twice, as the command does; nothing sent again, nothing seeded.
    assert _requests(world.trackers["OPS"], world.trackers["RED"]) == _requests(cli.source, cli.target)
    red = world.trackers["RED"]
    assert len(_target_uploads(red)) == 1
    lookups = [sent.query for sent in red.sent if sent.query.get("action") == "torrent"]
    assert len(lookups) == 2
    assert all("hash" in query for query in lookups)
    assert world.seeded == []


@pytest.mark.parametrize("stops", [False, True], ids=["the run goes on", "the run stops after it"])
def test_a_lossy_report_with_a_lost_answer_ends_the_job_as_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
    stops: bool,
) -> None:
    album = _album(dirs.downloads / FOLDER)
    world = cross_upload_world(monkeypatch, dirs, transcode=_transcode)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _holding(album, APPROVED_ID)(source, target)
        target.report_answers = ["drop"]
        if stops:
            target.uploads = ["ok", "Your torrent is too small"]

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, [str(APPROVED_ID)], params={"transcodes": ["V0"]}, assume_defaults=True)
        job, _asked = await _drive(client, manager, job_id)
        assert job.status == "unknown_outcome", (job.error, _log(job))
        assert job.error is not None
        assert job.error.startswith(f"{UNKNOWN_OUTCOME} (")
        assert any(line.startswith("Could not tell whether RED took the lossy") for line in _log(job))
        red = world.trackers["RED"].url
        uploaded = [f"{red}/torrents.php?torrentid={torrent_id}" for torrent_id in (700001, 700002)]
        assert [each["url"] for each in job.result["uploads"]] == uploaded[: 1 if stops else 2]
        if stops:
            assert "Stopping: API upload failed: Your torrent is too small. Nothing more is uploaded." in _log(job)

    _web(world, test, prepare)
    # Each report sent once, the lost one never again.
    reports = world.trackers["RED"].reports()
    assert [sent.fields["torrentid"] for sent in reports] == [["700001"], ["700002"]][: 1 if stops else 2]
    assert len(_target_uploads(world.trackers["RED"])) == 2


def test_a_failed_second_format_stops_the_job_and_its_result_links_what_is_up(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
) -> None:
    album = _album(dirs.downloads / FOLDER)
    world = cross_upload_world(monkeypatch, dirs, transcode=_transcode)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _holding(album)(source, target)
        target.uploads = ["ok", "Your torrent is too small"]

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, params={"transcodes": ["320", "V0"]}, assume_defaults=True)
        job, _asked = await _drive(client, manager, job_id)
        assert job.status == "failed", (job.error, _log(job))
        assert job.error == "API upload failed: Your torrent is too small"
        flac = f"{world.trackers['RED'].url}/torrents.php?torrentid=700001"
        assert job.result["uploads"] == [{"tracker": "RED", "format": "FLAC", "url": flac}]
        assert f"  {flac}" in _log(job)

    _web(world, test, prepare)
    assert len(_target_uploads(world.trackers["RED"])) == 2
    # What is up is still seeded.
    assert world.seeded == [("/seed", FOLDER)]


# --- Usage rules, inputs --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"target": "ops"}, "SOURCE_TRACKER and TARGET_TRACKER must be different trackers."),
        ({"target": "DIC"}, "Not configured: DIC. Add it under [tracker] in your config."),
        ({"source": "XYZ"}, "Not configured: XYZ. Add it under [tracker] in your config."),
        ({"inputs": []}, "Give at least one INPUT."),
        ({"inputs": [str(TORRENT_ID + n) for n in range(6)]}, "At most 5 releases per run, not 6."),
        ({"inputs": ["1", "2"], "group_id": 5}, "--path and --group-id go with a single INPUT."),
        ({"inputs": ["1", "2"], "path": "{album}"}, "--path and --group-id go with a single INPUT."),
        ({"inputs": ["{album}/release.torrent"]}, "{album}/release.torrent is not a OPS torrent ID or URL."),
        ({"inputs": ["https://elsewhere.test/torrents.php?torrentid=1"]}, "Expected a torrent URL from {OPS}, "),
        ({"inputs": ["{OPS}/torrents.php?id=500002"]}, "The torrent URL must hold a numeric torrentid."),
        ({"transcodes": ["V2"]}, "V2 is not a transcode salmon makes: 320 or V0."),
        ({"group_id": 0}, "Invalid parameters: Expected `int` >= 1"),
        ({"inputs": "600012"}, "Invalid parameters: Expected `array`, got `str`"),
        ({"torrent_file": "x"}, "Invalid parameters: Object contains unknown field `torrent_file`"),
    ],
)
def test_what_the_command_refuses_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
    params: dict[str, Any],
    detail: str,
) -> None:
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    album = _album(dirs.downloads / FOLDER)
    (album / "release.torrent").write_bytes(b"d4:infod4:name1:xee")
    world = cross_upload_world(monkeypatch, dirs)

    async def test(client: TestClient, manager: JobManager) -> None:
        def filled(value: Any) -> Any:
            if isinstance(value, list):
                return [filled(each) for each in value]
            if isinstance(value, str):
                return value.replace("{album}", str(album)).replace("{OPS}", world.trackers["OPS"].url)
            return value

        response = await _post(client, params={key: filled(value) for key, value in params.items()})
        assert response.status == 400, await response.text()
        assert (await response.json())["detail"].startswith(filled(detail))
        assert manager.jobs == {}

    _web(world, test)
    assert world.trackers["OPS"].sent == world.trackers["RED"].sent == []


def test_a_source_torrent_url_is_kept_as_the_id_it_names(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
) -> None:
    album = _album(dirs.downloads / FOLDER)
    world = cross_upload_world(monkeypatch, dirs)

    async def test(client: TestClient, manager: JobManager) -> None:
        url = f"{world.trackers['OPS'].url}/torrents.php?id=500002&torrentid={TORRENT_ID}&passkey=never-kept"
        job_id = await _start(client, [url], params={"source": "ops", "target": " red "}, assume_defaults=True)
        job = manager.jobs[job_id]
        assert job.title == f"Cross-upload OPS to RED: {TORRENT_ID}"
        assert job.shown_params["inputs"] == [str(TORRENT_ID)]
        job, _asked = await _drive(client, manager, job_id)
        assert job.status == "done", (job.error, _log(job))
        assert "never-kept" not in json.dumps(job.detail())

    _web(world, test, _holding(album))
    assert len(_target_uploads(world.trackers["RED"])) == 1


# --- Questions, -yyy -------------------------------------------------------------------------------------


def _two_releases(root: Path) -> tuple[Path, Path, Prepare]:
    """Two albums, and SOURCE holding a torrent of each, ids TORRENT_ID and TORRENT_ID + 1."""
    first, second = _album(root / f"{FOLDER} 1"), _album(root / f"{FOLDER} 2")

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _holding(first)(source, target)
        _holding(second, TORRENT_ID + 1)(source, target)
        # Each torrent has its row, with no lossy label, on its group's page.
        page = source.group_page
        source.group_page = page + page.replace(str(TORRENT_ID), str(TORRENT_ID + 1))

    return first, second, prepare


def test_assume_defaults_of_one_job_does_not_answer_anothers_questions(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
) -> None:
    _first, _second, prepare = _two_releases(dirs.downloads)
    world = cross_upload_world(monkeypatch, dirs)

    async def test(client: TestClient, manager: JobManager) -> None:
        asks = await _start(client, [str(TORRENT_ID)])
        assumes = await _start(client, [str(TORRENT_ID + 1)], assume_defaults=True)
        # The job without -yyy waits on the plan while the other runs through.
        await _until(lambda: manager.jobs[asks].question is not None)
        assumed, assumed_asked = await _drive(client, manager, assumes)
        assert assumed.status == "done", (assumed.error, _log(assumed))
        asked, asked_asked = await _drive(client, manager, asks)
        assert asked.status == "done", (asked.error, _log(asked))
        # -yyy answers the plan's yes or no; the group prompt is asked either way, as in the terminal.
        assert [question["kind"] for question in assumed_asked] == ["prompt"]
        assert [question["kind"] for question in asked_asked] == ["confirm", "prompt"]
        assert cfg.upload.yes_all is False

    _web(world, test, prepare)
    assert len(_target_uploads(world.trackers["RED"])) == 2


def test_the_log_check_asks_in_the_browser_and_its_no_drops_the_release(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
) -> None:
    # The command's own log check, its verdict faked: the log does not match the files.
    monkeypatch.setattr(cross_upload_module, "_check_logs", salmon.uploader._check_logs)

    async def mismatch(_log_path: str, _album: str) -> None:
        raise CRCMismatchError("track 1")

    monkeypatch.setattr(salmon.uploader, "check_log_cambia", mismatch)
    album = _album(dirs.downloads / FOLDER)
    (album / "rip.log").write_bytes(b"Exact Audio Copy V1.6 log")
    world = cross_upload_world(monkeypatch, dirs)

    def cd(answer: dict[str, Any]) -> None:
        answer["torrent"].update(media="CD", hasLog=True, logScore=100)

    async def test(client: TestClient, manager: JobManager) -> None:
        job, asked = await _drive(client, manager, await _start(client))
        assert [click.unstyle(question["text"]) for question in asked] == [
            "Log file CRC does not match audio files. Do you want to continue upload anyway?"
        ]
        assert job.status == "failed", (job.error, _log(job))
        assert job.error == "Nothing to cross-upload."
        assert f"Not cross-uploading {TORRENT_ID}: the log check stopped it" in _log(job)

    _web(world, test, _holding(album, change=cd))
    assert world.trackers["RED"].sent == []


# --- One job per album folder -------------------------------------------------------------------------------


def _probe(monkeypatch: pytest.MonkeyPatch, started: list[str]) -> None:
    """A job kind working on a folder, which notes when it starts."""

    class Params(msgspec.Struct):
        folder: str

    async def run(params: Params) -> None:
        started.append(params.folder)

    monkeypatch.setitem(
        jobs.KINDS,
        "probe",
        JobKind(name="probe", params=Params, run=run, title=lambda _p: "probe", folder=lambda p: p.folder),
    )


def test_without_a_path_the_folder_is_held_once_source_names_it_and_a_second_job_on_it_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
) -> None:
    album = _album(dirs.downloads / FOLDER)
    world = cross_upload_world(monkeypatch, dirs)
    started: list[str] = []
    _probe(monkeypatch, started)

    async def test(client: TestClient, manager: JobManager) -> None:
        # The first job reads SOURCE, holds the folder, and waits on its plan.
        first = await _start(client)
        await _until(lambda: manager.jobs[first].question is not None)
        assert len(world.trackers["OPS"].sent) == 4
        # A second cross-upload of the same release learns its folder only from SOURCE: it is not cross-uploaded,
        # as a release that fails a check is not, and its job ends.
        second, _asked = await _drive(client, manager, await _start(client, assume_defaults=True))
        assert second.status == "failed", (second.error, _log(second))
        assert second.error == "Nothing to cross-upload."
        assert (
            f"Not cross-uploading {TORRENT_ID}: another job works on {album} (Cross-upload OPS to RED: {TORRENT_ID}): "
            "cross-upload it once that job has ended" in _log(second)
        )
        # A job started on the folder waits for the first to end.
        probe = await client.post("/api/jobs", json={"kind": "probe", "params": {"folder": str(album)}}, headers=AUTH)
        probe_id = (await probe.json())["id"]
        await asyncio.sleep(0.2)
        assert (manager.jobs[probe_id].status, started) == ("queued", [])
        job, _asked = await _drive(client, manager, first)
        assert job.status == "done", (job.error, _log(job))
        assert (await _finished(manager, probe_id)).status == "done"
        assert started == [str(album)]

    _web(world, test, _holding(album))
    # The second job read the torrent, then sent nothing more: no download, no group page, nothing to TARGET.
    steps = [sent.step for sent in world.trackers["OPS"].sent]
    assert steps.count("GET ajax.php?action=torrent") == 2
    assert steps.count("GET ajax.php?action=download") == 1
    assert len(_target_uploads(world.trackers["RED"])) == 1


def test_with_a_path_a_second_job_on_that_folder_waits_its_turn(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
) -> None:
    album = _album(dirs.downloads / "elsewhere" / FOLDER)
    world = cross_upload_world(monkeypatch, dirs)

    async def test(client: TestClient, manager: JobManager) -> None:
        first = await _start(client, params={"path": str(album)})
        await _until(lambda: manager.jobs[first].question is not None)
        second = await _start(client, params={"path": str(album)}, dry_run=True, assume_defaults=True)
        await asyncio.sleep(0.2)
        assert manager.jobs[second].status == "queued"
        job, _asked = await _drive(client, manager, first)
        assert job.status == "done", (job.error, _log(job))
        job, _asked = await _drive(client, manager, second)
        assert job.status == "done", (job.error, _log(job))

    _web(world, test, _holding(album))
    assert len(_target_uploads(world.trackers["RED"])) == 1


# --- Library albums ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True], ids=["upload", "dry run"])
def test_a_library_album_is_read_where_it_is_and_never_written(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
    dry_run: bool,
) -> None:
    album = _album(dirs.library / "sample3000" / FOLDER)
    before = _snapshot(album)
    world = cross_upload_world(monkeypatch, dirs, transcode=_transcode)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(
            client, params={"path": str(album), "transcodes": ["V0"]}, dry_run=dry_run, assume_defaults=True
        )
        job, _asked = await _drive(client, manager, job_id)
        assert job.status == "done", (job.error, _log(job))
        assert len(job.result["uploads"]) == (0 if dry_run else 2)

    _web(world, test, _holding(album))
    # Byte for byte, mtimes too, and nothing beside it.
    assert _snapshot(album) == before
    assert sorted(path.name for path in album.parent.iterdir()) == [FOLDER]
    assert sorted(path.name for path in dirs.library.iterdir()) == ["sample3000"]
    # The transcode and the torrents went where the command puts them; a dry run's scratch is gone.
    if dry_run:
        assert sorted(os.listdir(dirs.downloads)) == [staging.STAGING_DIR]
        assert os.listdir(dirs.downloads / staging.STAGING_DIR) == []
        assert list(dirs.torrents.iterdir()) == []
    else:
        assert sorted(os.listdir(dirs.downloads)) == [f"{FOLDER} [V0]"]
        assert sorted(path.name for path in dirs.torrents.iterdir()) == [
            f"{FOLDER} - RED.torrent",
            f"{FOLDER} [V0] - RED.torrent",
        ]
        assert world.seeded[0] == ("/seed", FOLDER)


# --- Through the request loop, beside an upload --------------------------------------------------------------


class TimedTracker(FakeTracker):
    """The fake tracker, noting when each request arrives, and when each POST, answered slowly, starts and ends."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.arrivals: list[float] = []
        self.post_spans: list[tuple[float, float]] = []

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.arrivals.append(time.monotonic())
        if request.method != "POST":
            return await super()._handle(request)
        started = time.monotonic()
        try:
            await asyncio.sleep(0.3)
            return await super()._handle(request)
        finally:
            self.post_spans.append((started, time.monotonic()))


def test_a_cross_upload_and_an_upload_to_one_account_share_its_budget_and_post_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
) -> None:
    # A one second period, with a margin as large for it as the real one is for 10 s: only the times change.
    monkeypatch.setattr(account, "RATE_LIMIT_PERIOD", 1.0)
    monkeypatch.setattr(account, "RATE_LIMIT_MARGIN", 0.2)
    cross_album = _album(dirs.downloads / FOLDER)
    up_album = up_world._album(dirs.downloads / "Album")
    world = cross_upload_world(monkeypatch, dirs)
    # One fake RED for both jobs: what the account sends, measured where it arrives.
    red = world.trackers["RED"] = TimedTracker("RED")
    # The upload's stubs; its tracker is this fake, which answers what it asks but the requests check.
    up_world.fake_upload_world(monkeypatch, dirs.torrents, multi_tracker_upload=False, tracker=cast("Any", red))
    monkeypatch.setattr(cfg.upload.requests, "check_requests", False)
    monkeypatch.setattr(cfg.image, "specs_uploader", "testhost")
    # The group each upload goes into, for the formats it holds.
    red.groups[TARGET_GROUP_ID] = up_world._group()
    # Both jobs make their RED client, in their own threads, before either sends RED anything.
    made: set[str] = set()
    both = threading.Condition()

    def client(code: str) -> BaseGazelleApi:
        job = jobs._running_job.get()
        if code == "RED" and job is not None:
            with both:
                made.add(job.id)
                both.notify_all()
                assert both.wait_for(lambda: len(made) == 2, timeout=30)
        # The account's own rate limit, on salmon web's request loop.
        return _client(code, world.trackers[code], dirs.torrents, unlimited=False)

    monkeypatch.setattr(salmon.trackers, "get_class", lambda code: lambda: client(code))

    async def test(client: TestClient, manager: JobManager) -> None:
        cross = await _start(client, assume_defaults=True)
        up = await client.post(
            "/api/jobs",
            json={
                "kind": "upload",
                "params": {"path": str(up_album), "trackers": ["RED"], **UP_PARAMS},
                "assume_defaults": True,
            },
            headers=AUTH,
        )
        assert up.status == 201, await up.text()
        up_id = (await up.json())["id"]
        ended = await asyncio.gather(*(_drive(client, manager, job_id) for job_id in (cross, up_id)))
        for job, _asked in ended:
            assert job.status == "done", (job.error, _log(job))

    _web(world, test, _holding(cross_album))
    # The cross-upload's FLAC; the upload's FLAC and two transcodes, each reported.
    assert len(_target_uploads(red)) == 4
    assert len(red.reports()) == 3
    # One budget: no more than the limit within any period, though far more were sent.
    assert len(red.arrivals) > 2 * account.RATE_LIMIT_REQUESTS
    assert _most_within(red.arrivals, 1.0) <= account.RATE_LIMIT_REQUESTS
    # One POST in flight at a time.
    spans = sorted(red.post_spans)
    assert len(spans) == 7
    assert all(later[0] >= earlier[1] for earlier, later in zip(spans, spans[1:], strict=False))


# --- Secrets ----------------------------------------------------------------------------------------------------


PLANTED = {
    "red_session": "planted-red-session-5a1f",
    "red_api_key": "planted-red-api-key-77c2",
    "ops_session": "planted-ops-session-0b9e",
    "ops_api_key": "planted-ops-api-key-c41d",
    "image_key": "planted-imgbb-key-e3a8",
}


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry run", "upload"])
def test_no_planted_secret_leaves_in_any_event_of_a_cross_upload_job(
    monkeypatch: pytest.MonkeyPatch,
    dirs: SimpleNamespace,  # noqa: F811
    dry_run: bool,
) -> None:
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", True)
    for code in ("red", "ops"):
        planted = GazelleTrackerSettings(session=PLANTED[f"{code}_session"], api_key=PLANTED[f"{code}_api_key"])
        monkeypatch.setattr(cfg.tracker, code, planted)
        monkeypatch.setitem(API_KEYS, code.upper(), PLANTED[f"{code}_api_key"])
        monkeypatch.setitem(SESSIONS, code.upper(), PLANTED[f"{code}_session"])
    monkeypatch.setattr(cfg.image, "imgbb_key", PLANTED["image_key"])
    # Every line naming the album, the torrent or a form's file holds secrets: the folder's name does.
    album = _album(dirs.downloads / f"{FOLDER} {PLANTED['ops_session']} {PLANTED['image_key']}")
    world = cross_upload_world(monkeypatch, dirs, transcode=_transcode)

    async def test(client: TestClient, manager: JobManager) -> None:
        events: list[Any] = []
        async with client.ws_connect("/api/ws", headers=AUTH) as socket:
            job_id = await _start(client, params={"transcodes": ["V0"]}, dry_run=dry_run, assume_defaults=True)
            job, _asked = await _drive(client, manager, job_id)
            assert job.status == "done", (job.error, _log(job))
            while not any(event["event"] == "finished" for event in events):
                message = await socket.receive(timeout=5)
                assert message.type == WSMsgType.TEXT
                events.append(json.loads(message.data))
            sent = [json.dumps(events)]
            sent.append(await (await client.get("/api/jobs", headers=AUTH)).text())
            sent.append(await (await client.get(f"/api/jobs/{job_id}", headers=AUTH)).text())
        everything = "\n".join(sent)
        assert "[DEBUG] params" in everything
        assert ("Dry run: not uploading to RED" in everything) is dry_run
        assert "[REDACTED]" in everything
        for name, secret in {**PLANTED, "authkey": AUTHKEY, "passkey": PASSKEY}.items():
            assert secret not in everything, name

    _web(world, test, _holding(album))
    assert len(_target_uploads(world.trackers["RED"])) == (0 if dry_run else 2)
