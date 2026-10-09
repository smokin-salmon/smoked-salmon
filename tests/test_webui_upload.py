"""salmon web's upload job: the same pipeline as salmon up, through the HTTP API (#633, ADR 0004).

Every upload goes to a local fake tracker on 127.0.0.1 and every image to a fake image host: nothing leaves the
machine (the network guard in conftest.py stands). The runs stub what needs audio tools, a metadata source or a
reviewer, as tests/test_uploader_dry_run.py does for salmon up, and build the real staging copy, torrents and forms.

The idea of driving whole uploads through the web against a fake Gazelle tracker is styx-techno's (the fork's
tests/fake_gazelle.py and test_webui_upload_http_integration.py, 7abfb9d4); the harness here is salmon up's own.
"""

import asyncio
import json
import os
import stat
import sys
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any, cast

import anyio
import asyncclick as click
import msgspec
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient
from tenacity import wait_none
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    AUTHKEY,
    GROUP_ID,
    PASSKEY,
    SOURCE_URL,
    FakeTracker,
    Sent,
    _album,
    _run_up,
    _snapshot,
    fake_upload_world,
    image_uploads,  # noqa: F401
)
from test_webui_jobs import AUTH, _finished, _log, _until, _with_app  # pyright: ignore[reportMissingImports]

import salmon.trackers
import salmon.uploader
from salmon import cfg
from salmon.common import handle_scrape_errors
from salmon.config.validations import GazelleTrackerSettings, Seedbox
from salmon.errors import UnknownOutcomeError
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import seedbox, spectrals, staging
from salmon.uploader.record import recording
from salmon.webui import jobs
from salmon.webui.jobs import FINISHED, Job, JobKind, JobManager

pytestmark = pytest.mark.usefixtures("image_uploads")

# What _run_up gives salmon up besides the album and -t: -s WEB -n --skip-integrity-check --skip-up --source-url.
PARAMS = {
    "source": "WEB",
    "auto_rename": True,
    "skip_integrity_check": True,
    "skip_up": True,
    "source_url": SOURCE_URL,
}


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the authkeys and passkeys other tests' fake trackers gave, short ones among them."""
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture
def roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    """A download_directory, a library and a dot_torrents_dir, configured."""
    downloads, library, torrents = tmp_path / "downloads", tmp_path / "library", tmp_path / "torrents"
    for folder in (downloads, library, torrents):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    return downloads, library, torrents


# --- Running an upload job --------------------------------------------------------------------


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


def _uploads(tracker: FakeTracker) -> list[Sent]:
    """The uploads the tracker got: through the API, or through the site's form (RED takes a CD's logs there)."""
    return [
        sent
        for sent in tracker.sent
        if sent.method == "POST"
        and (sent.path == "/upload.php" or (sent.path, sent.query.get("action")) == ("/ajax.php", "upload"))
    ]


def _as_sent(tracker: FakeTracker) -> list[tuple[str, str, dict[str, str], list[tuple[str, Any]]]]:
    """What the tracker got, its own address left out of the values: each run's fake listens on another port."""

    def value(field: Any) -> Any:
        return field.replace(tracker.url, "<tracker>") if isinstance(field, str) else field

    return [
        (sent.method, sent.path, sent.query, [(name, value(field)) for name, field in sent.fields])
        for sent in tracker.sent
    ]


# --- The same requests as salmon up -------------------------------------------------------------


# The metadata _run_up's stubs give, from a CD.
CD_METADATA = {
    "format": "FLAC",
    "encoding": "Lossless",
    "artists": [("Artist", "main")],
    "title": "Album",
    "catno": "CAT1",
    "source": "CD",
    "year": 2020,
    "group_year": 2020,
    "date": "2020-01-01",
    "label": "Label",
    "rls_type": "Album",
    "edition_title": None,
    "encoding_vbr": False,
    "scene": False,
    "cover": None,
    "comment": None,
    "urls": [],
    "genres": ["Electronic"],
}


async def _cd_metadata(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], None]:
    return CD_METADATA, None


async def _cd_reviewed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return CD_METADATA


@pytest.mark.parametrize("source", ["WEB", "CD"])
def test_a_web_upload_sends_what_salmon_up_sends(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],
    tmp_path: Path,
    image_uploads: list[tuple[str, str]],  # noqa: F811
    source: str,
) -> None:
    downloads, _library, torrents = roots
    fakes = {"get_metadata": _cd_metadata, "review_metadata_with_ai": _cd_reviewed} if source == "CD" else {}
    cd_options = ("-s", "CD", "--skip-log-check") if source == "CD" else ()

    def rip(folder: Path) -> Path:
        album = _album(folder)
        if source == "CD":
            (album / "Album.log").write_bytes(b"Exact Audio Copy V1.6 log")
        return album

    # salmon up -t RED -t OPS, every question answered with its default (an empty line), in a download_directory of
    # its own: its renamed album stays there.
    cli_downloads = tmp_path / "cli-downloads"
    cli_downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(cli_downloads))
    cli = _run_up(
        monkeypatch,
        rip(cli_downloads / "Album"),
        torrents,
        args=cd_options,
        input="\n" * 30,
        yes_all=False,
        trackers=("RED", "OPS"),
        **fakes,
    )
    assert cli.result.exit_code == 0, cli.result.output
    cli_images = list(image_uploads)
    image_uploads.clear()

    # The same album and answers, as a web job.
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    album = rip(downloads / "Album")
    tracker, queued, _logins = fake_upload_world(monkeypatch, torrents, **fakes)
    params = {"trackers": ["RED", "OPS"], **({"source": "CD", "skip_log_check": True} if source == "CD" else {})}

    async def test(client: TestClient, manager: JobManager) -> None:
        job, asked = await _drive(client, manager, await _start(client, album, params=params))
        assert job.status == "done", (job.error, _log(job))
        # Each question the command asked in the terminal, in the same order.
        shown = click.unstyle(cli.result.output)
        at = 0
        for question in asked:
            at = shown.index(click.unstyle(question["text"]).strip(), at)
        assert len(asked) > 5
        # Without the terminal's colour codes, as the log shows its lines.
        assert not [question for question in asked if "\x1b" in question["text"]]
        assert job.result["trackers"] == ["RED", "OPS"]
        # Uploaded in order, each torrent linked: the FLAC and its two transcodes on RED, then on OPS.
        assert [(each["tracker"], each["format"]) for each in job.result["uploads"]] == [
            (code, format) for code in ("RED", "OPS") for format in ("FLAC", "MP3", "MP3")
        ]
        assert all(each["url"].startswith(f"{tracker.url}/torrents.php?torrentid=") for each in job.result["uploads"])

    _web_run(tracker, test)

    assert _as_sent(tracker) == _as_sent(cli.tracker)
    # The real upload forms: the torrent file, and with a CD's FLAC its rip log.
    uploads = _uploads(tracker)
    assert len(uploads) == 6
    for sent in uploads:
        files = {name: value for name, value in sent.fields if isinstance(value, tuple)}
        assert files["file_input"][0].endswith(".torrent")
        assert files["file_input"][1].startswith(b"d")
        if source == "CD" and dict(sent.fields)["format"] == "FLAC":
            assert files["logfiles[]"] == ("Album.log", b"Exact Audio Copy V1.6 log")
        else:
            assert "logfiles[]" not in files
    # The cover and the spectrals went to the fake image host, as from the command.
    assert image_uploads == cli_images != []
    assert len(queued) == len(cli.queued)


def test_a_web_upload_into_a_group_sends_what_salmon_up_sends(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],
    tmp_path: Path,
    image_uploads: list[tuple[str, str]],  # noqa: F811
) -> None:
    downloads, _library, torrents = roots
    # The command takes -g as a string, the job its group ID as a number: the requests are the same.
    cli_downloads = tmp_path / "cli-downloads"
    cli_downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(cli_downloads))
    cli = _run_up(
        monkeypatch,
        _album(cli_downloads / "Album"),
        torrents,
        args=("-g", str(GROUP_ID), "--request", "41"),
        input="\n" * 30,
        multi_tracker_upload=False,
    )
    assert cli.result.exit_code == 0, cli.result.output
    image_uploads.clear()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    album = _album(downloads / "Album")
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents, multi_tracker_upload=False)
    params = {"trackers": ["RED"], "group_id": GROUP_ID, "request": "41"}

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(client, manager, await _start(client, album, params=params, assume_defaults=True))
        assert job.status == "done", (job.error, _log(job))

    _web_run(tracker, test)
    assert _as_sent(tracker) == _as_sent(cli.tracker)
    first = dict(_uploads(tracker)[0].fields)
    assert (first["groupid"], first["requestid"]) == (str(GROUP_ID), "41")


def test_the_form_offers_the_trackers_sources_and_encodings_up_takes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    tracker = FakeTracker()

    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await client.get("/api/upload/options", headers=AUTH)
        assert response.status == 200
        assert await response.json() == {
            "trackers": ["RED", "OPS"],
            "sources": ["WEB", "CD", "DVD", "Vinyl", "Soundboard", "SACD", "DAT", "Cassette"],
            "encodings": ["V0", "V1", "V2", "320", "256", "192", "320V", "256V", "192V"],
        }

    _web_run(tracker, test)
    assert tracker.sent == []


# --- Dry run, -yyy, several trackers --------------------------------------------------------------


def test_a_dry_run_job_sends_no_post_and_prints_the_forms(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],
    image_uploads: list[tuple[str, str]],  # noqa: F811
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    before = _snapshot(album)
    tracker, queued, logins = fake_upload_world(monkeypatch, torrents, multi_tracker_upload=False)

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(
            client, manager, await _start(client, album, params={"trackers": ["RED"]}, dry_run=True)
        )
        assert job.status == "done", (job.error, _log(job))
        assert job.dry_run is True
        assert job.result == {"folder": "Album", "trackers": ["RED"], "uploads": []}
        log = _log(job)
        would_send = (
            f"Dry run: not uploading to RED. It would send POST {tracker.url}/ajax.php?action=upload with the API key:"
        )
        assert log.count(would_send) == 3
        # Each form, part by part, and the torrent it holds.
        assert log.count("  auth: [REDACTED]") == 3
        assert len([line for line in log if line.startswith("  file_input: ") and ".torrent (" in line]) == 3
        assert len([line for line in log if line.startswith("  The torrent: ")]) == 3
        assert f"Dry run: done. Nothing was sent, and {album} is unchanged." in log

    _web_run(tracker, test)

    assert tracker.not_gets() == []
    assert {(sent.path, sent.query["action"]) for sent in tracker.sent} == {
        ("/ajax.php", "index"),
        ("/ajax.php", "browse"),
        ("/ajax.php", "requests"),
    }
    assert image_uploads == queued == logins == []
    assert _snapshot(album) == before
    assert os.listdir(downloads / staging.STAGING_DIR) == []


def test_assume_defaults_of_one_job_does_not_answer_anothers_questions(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    downloads, _library, torrents = roots
    first, second = _album(downloads / "First" / "Album"), _album(downloads / "Second" / "Album")
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents, multi_tracker_upload=False)

    async def test(client: TestClient, manager: JobManager) -> None:
        asks = await _start(client, second, params={"trackers": ["RED"]})
        assumes = await _start(client, first, params={"trackers": ["RED"]}, assume_defaults=True)
        # The job without -yyy waits on its first question while the other runs through.
        await _until(lambda: manager.jobs[asks].question is not None)
        assumed, assumed_asked = await _drive(client, manager, assumes)
        assert assumed.status == "done", (assumed.error, _log(assumed))
        asked, asked_asked = await _drive(client, manager, asks)
        assert asked.status == "done", (asked.error, _log(asked))
        # -yyy answers the yes or no questions; the group prompt is asked either way, as in the terminal.
        assert [question["kind"] for question in assumed_asked] == ["prompt"]
        assert [question["kind"] for question in asked_asked].count("confirm") == 4
        assert cfg.upload.yes_all is False

    _web_run(tracker, test)
    assert len(_uploads(tracker)) == 6


def test_a_two_tracker_job_uploads_to_each_in_order(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    tracker, queued, _logins = fake_upload_world(monkeypatch, torrents)
    seen: list[str] = []
    original = salmon.trackers.get_class

    def get_class(code: str) -> Any:
        seen.append(code)
        return original(code)

    monkeypatch.setattr(salmon.trackers, "get_class", get_class)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, album, params={"trackers": ["red", "OPS", "RED"]}, assume_defaults=True)
        assert manager.jobs[job_id].title == "Upload to RED, OPS: Album"
        job, asked = await _drive(client, manager, job_id)
        assert job.status == "done", (job.error, _log(job))
        # Two group prompts, and no question about another tracker: the job names its trackers.
        assert len(asked) == 2
        assert [each["tracker"] for each in job.result["uploads"]] == ["RED"] * 3 + ["OPS"] * 3

    _web_run(tracker, test)
    assert seen[0] == "RED"
    assert "OPS" in seen
    assert len(_uploads(tracker)) == 6
    # Each upload's folder and torrent queued for the seedbox.
    assert len(queued) == 12


# --- Tracker answers: a 429, a lost answer -------------------------------------------------------


Answer = Callable[[], web.StreamResponse]


class AnsweringTracker(FakeTracker):
    """The fake tracker, with the first upload and report POSTs answered as told; then as usual."""

    def __init__(self, uploads: tuple[Answer, ...] = (), reports: tuple[Answer, ...] = ()) -> None:
        super().__init__()
        self._answers = {("/ajax.php", "upload"): list(uploads), ("/reportsv2.php", "takereport"): list(reports)}
        self.lookups: list[str] = []

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        action = request.query.get("action")
        if request.path == "/ajax.php" and action == "torrent":
            self.sent.append(Sent(request.method, request.path, dict(request.query), []))
            self.lookups.append(request.query["hash"])
            return web.json_response({"status": "failure", "error": "bad hash"})
        answers = self._answers.get((request.path, action or ""))
        if request.method == "POST" and answers:
            fields = []
            for name, value in (await request.post()).items():
                is_file = isinstance(value, web.FileField)
                fields.append((name, (value.filename, value.file.read()) if is_file else value))
            self.sent.append(Sent(request.method, request.path, dict(request.query), fields))
            return answers.pop(0)()
        return await super()._handle(request)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without the backoff wait; a 429 still waits the 2 s it asks for."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


def test_a_429_on_the_upload_is_waited_for_and_sent_once_more(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], no_backoff: None
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    tracker = AnsweringTracker(
        uploads=(lambda: web.json_response({"status": "failure"}, status=429, headers={"Retry-After": "1"}),)
    )
    fake_upload_world(monkeypatch, torrents, multi_tracker_upload=False, tracker=tracker)
    # The loop each request body is made on: the form is built on the job's loop, its body on the request loop.
    bodies: list[tuple[asyncio.AbstractEventLoop, str]] = []
    stall_bound_body = base._stall_bound_body

    async def recorded(data: Any, stall_secs: int) -> Any:
        body = await stall_bound_body(data, stall_secs)
        bodies.append((asyncio.get_running_loop(), body.content_type))
        return body

    monkeypatch.setattr(base, "_stall_bound_body", recorded)

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(client, manager, await _start(client, album, params={"trackers": ["RED"]}))
        assert job.status == "done", (job.error, _log(job))
        assert "Rate limit exceeded, waiting 2 seconds..." in _log(job)
        assert len(job.result["uploads"]) == 3
        # Every upload and report went out from salmon web's own loop, the one serving this test.
        server_loop = asyncio.get_running_loop()
        assert [loop for loop, _type in bodies if loop is not server_loop] == []
        assert len([kind for _loop, kind in bodies if kind.startswith("multipart/form-data")]) == 4

    _web_run(tracker, test)
    uploads = _uploads(tracker)
    # The FLAC twice, the very same form; then its two transcodes.
    assert len(uploads) == 4
    assert uploads[0].fields == uploads[1].fields
    assert uploads[2].fields != uploads[1].fields


def test_an_answer_lost_after_the_upload_ends_the_job_as_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], no_backoff: None
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    monkeypatch.setattr(base, "_LOST_UPLOAD_FIRST_WAIT", 0)
    monkeypatch.setattr(base, "_LOST_UPLOAD_SECOND_WAIT", 0)
    tracker = AnsweringTracker(uploads=(lambda: web.Response(status=502, text="Bad gateway"),))
    fake_upload_world(monkeypatch, torrents, multi_tracker_upload=False, tracker=tracker)

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(client, manager, await _start(client, album, params={"trackers": ["RED"]}))
        assert job.status == "unknown_outcome", (job.error, _log(job))
        assert job.error is not None
        assert job.error.startswith("The request may have reached the tracker: check the site before trying again.")
        assert "check your uploads on RED before uploading it again" in job.error
        assert job.result is None
        assert any(
            line.startswith("Upload to RED failed: Could not tell whether RED took the upload") for line in _log(job)
        )

    _web_run(tracker, test)
    # Sent once, never again: looked up by its infohash twice, as salmon up does.
    assert len(_uploads(tracker)) == 1
    assert len(tracker.lookups) == 2
    assert tracker.lookups[0] == tracker.lookups[1]


def test_a_lossy_report_with_a_lost_answer_ends_the_job_as_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], no_backoff: None
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    tracker = AnsweringTracker(reports=(lambda: web.Response(status=502, text="Bad gateway"),))
    fake_upload_world(monkeypatch, torrents, multi_tracker_upload=False, tracker=tracker)

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(client, manager, await _start(client, album, params={"trackers": ["RED"]}))
        assert job.status == "unknown_outcome", (job.error, _log(job))
        assert job.error is not None
        assert job.error.startswith("The request may have reached the tracker: check the site before trying again.")
        # The run went on, as salmon up does: the transcodes were uploaded and reported.
        assert any(line.startswith("Could not tell whether RED took the lossy master report") for line in _log(job))
        assert len([line for line in _log(job) if line.startswith("Successfully uploaded")]) == 3

    _web_run(tracker, test)
    reports = [sent for sent in tracker.sent if sent.path == "/reportsv2.php"]
    assert len(reports) == 3
    assert len({dict(sent.fields)["torrentid"] for sent in reports}) == 3


def test_a_description_edit_with_a_lost_answer_is_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lost = UnknownOutcomeError("RED answered 502 on a later hop")

    class Site:
        site_code = site_string = "RED"
        base_url = "http://127.0.0.1:9"

        async def append_to_torrent_description(self, *_args: Any) -> None:
            raise lost

    class Uploads:
        def hosts_text(self) -> str:
            return "testhost"

        async def urls_for(self, *_args: Any) -> dict[int, list[str]]:
            return {1: ["https://images.test/1.png", "https://images.test/2.png"]}

    async def checked(*_args: Any, **_kwargs: Any) -> tuple[bool, dict[int, str]]:
        return False, {1: "01. one.flac"}

    monkeypatch.setattr(spectrals, "check_spectrals", checked)
    track_data = {"01. one.flac": {}}
    site, uploads = cast("Any", Site()), cast("Any", Uploads())
    with recording() as record:
        anyio.run(
            partial(
                spectrals.post_upload_spectral_check,
                site,
                str(tmp_path),
                77,
                None,
                track_data,
                "WEB",
                None,
                uploads=uploads,
            )
        )
    assert record.unknown_outcomes == [lost]


# --- Library albums, refused folders, usage rules -------------------------------------------------


def test_a_library_album_is_uploaded_from_a_copy_and_left_as_it_was(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    downloads, library, torrents = roots
    album = _album(library / "Artist" / "Album")
    before = _snapshot(album)
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents, multi_tracker_upload=False)

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(
            client, manager, await _start(client, album, params={"trackers": ["RED"]}, assume_defaults=True)
        )
        assert job.status == "done", (job.error, _log(job))
        assert len(job.result["uploads"]) == 3
        assert any(line.startswith(f"Copying {album} ") for line in _log(job))

    _web_run(tracker, test)
    assert _snapshot(album) == before
    # The renamed copy stays in download_directory, to be seeded.
    assert (downloads / "Artist - Album (2020) [WEB FLAC]" / "01. one.flac").is_file()


def test_a_folder_the_rule_refuses_never_starts(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    downloads, library, torrents = roots
    outside = _album(tmp_path / "outside" / "Album")
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents)

    async def test(client: TestClient, manager: JobManager) -> None:
        for path, status in ((outside, 403), (downloads, 403), (library, 403), (downloads / "missing", 404)):
            response = await _post_upload(client, path)
            assert response.status == status, (path, await response.text())
        assert manager.jobs == {}

    _web_run(tracker, test)
    assert tracker.sent == []


@pytest.mark.parametrize(
    ("params", "dry_run", "detail"),
    [
        ({"skip_flac_upload": True}, False, "--skip-flac-upload requires --group-id."),
        (
            {"skip_flac_upload": True, "group_id": 5, "request": "7"},
            False,
            "--skip-flac-upload cannot be used with --request.",
        ),
        (
            {"skip_flac_upload": True, "group_id": 5, "trackers": ["RED", "OPS"]},
            False,
            "--skip-flac-upload uploads to one tracker: give -t a single tracker.",
        ),
        ({"essential_only": True, "scene": True}, False, "--essential-only and --scene cannot be used together."),
        ({"spectrals_after": True}, True, "--dry-run cannot be used with --spectrals-after"),
        ({"source": "Tape"}, False, "Tape is not a valid source."),
        ({"encoding": "V9"}, False, "V9 is not a valid encoding."),
        ({"trackers": ["XYZ"]}, False, "XYZ is not a tracker in your config."),
    ],
)
def test_the_job_refuses_what_salmon_up_refuses(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], params: dict[str, Any], dry_run: bool, detail: str
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents)

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post_upload(client, album, params=params, dry_run=dry_run)
        assert response.status == 400
        assert (await response.json())["detail"].startswith(detail)
        assert manager.jobs == {}

    _web_run(tracker, test)
    assert tracker.sent == []


@pytest.mark.parametrize("params", [{"source": None}, {"group_id": 0}, {"spectrals": [0]}, {"unknown": True}])
def test_parameters_a_form_cannot_give_are_refused(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], params: dict[str, Any]
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents)

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post_upload(client, album, params=params)
        assert response.status == 400, await response.text()
        assert manager.jobs == {}

    _web_run(tracker, test)


# --- Output: scrape tracebacks, rclone ---------------------------------------------------------------


def _kind(monkeypatch: pytest.MonkeyPatch, name: str, run: Callable[[Any], Awaitable[Any]]) -> None:
    class Params(msgspec.Struct):
        pass

    monkeypatch.setitem(jobs.KINDS, name, JobKind(name=name, params=Params, run=run, title=lambda _p: name))


def test_a_scrape_traceback_goes_to_the_servers_stderr_not_the_jobs_log(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    async def broken() -> None:
        raise RuntimeError("the store page changed")

    async def run(_params: Any) -> None:
        assert await handle_scrape_errors(broken()) is None

    _kind(monkeypatch, "scrapes", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await client.post("/api/jobs", json={"kind": "scrapes"}, headers=AUTH)
        job = await _finished(manager, (await response.json())["id"])
        assert job.status == "done"
        assert _log(job) == ["Unexpected scrape error: the store page changed"]

    _with_app(test)
    err = capfd.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: the store page changed" in err


RCLONE_SECRET = "planted-sftp-pass-61d0"


def test_rclones_stdout_reaches_the_jobs_log_masked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "rclone"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"sys.stdout.write('Transferred: 1 / 2, 50%\\rTransferred: 2 / 2, 100%\\n')\n"
        f"sys.stdout.write('ERROR : cannot reach sftp --sftp-pass {RCLONE_SECRET}\\n')\n"
        f"sys.stdout.flush()\n"
        f"sys.stderr.write('NOTICE: config uses --sftp-pass {RCLONE_SECRET}\\n')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    box = Seedbox(url="sbox", extra_args=["--sftp-pass", RCLONE_SECRET])
    album = tmp_path / "Album"
    album.mkdir()

    async def run(_params: Any) -> bool:
        return await seedbox._rclone_upload_folder(box, "/remote", str(album))

    _kind(monkeypatch, "copies", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await client.post("/api/jobs", json={"kind": "copies"}, headers=AUTH)
        job = await _finished(manager, (await response.json())["id"])
        assert job.status == "done" and job.result is True
        lines = [(line["text"], line["err"]) for line in job.log]
        assert ("Transferred: 2 / 2, 100%", False) in lines
        assert ("ERROR : cannot reach sftp --sftp-pass [REDACTED]", False) in lines
        assert ("NOTICE: config uses --sftp-pass [REDACTED]", True) in lines

    _with_app(test)
    out, err = capfd.readouterr()
    assert RCLONE_SECRET not in out + err
    assert "Transferred" not in out


# --- Secrets ------------------------------------------------------------------------------------------

PLANTED = {
    "session": "planted-session-cookie-81fa",
    "api_key": "planted-red-api-key-9d3c",
    "image_key": "planted-imgbb-key-27e0",
}


def test_no_planted_secret_leaves_in_any_event_of_an_upload_job(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    downloads, _library, torrents = roots
    monkeypatch.setattr(
        cfg.tracker, "red", GazelleTrackerSettings(session=PLANTED["session"], api_key=PLANTED["api_key"])
    )
    monkeypatch.setattr(cfg.image, "imgbb_key", PLANTED["image_key"])
    # Every name the run prints holds a secret: the folder, and so the torrent and the forms.
    album = _album(downloads / f"Album {PLANTED['api_key']}")
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents, multi_tracker_upload=False)

    async def test(client: TestClient, manager: JobManager) -> None:
        events: list[Any] = []
        async with client.ws_connect("/api/ws", headers=AUTH) as socket:
            job_id = await _start(
                client,
                album,
                params={"trackers": ["RED"], "source_url": f"{SOURCE_URL}?key={PLANTED['session']}"},
                dry_run=True,
            )
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
        assert "Dry run: not uploading to RED" in everything
        assert "[REDACTED]" in everything
        for name, secret in {**PLANTED, "authkey": AUTHKEY, "passkey": PASSKEY}.items():
            assert secret not in everything, name

    _web_run(tracker, test)
    assert tracker.not_gets() == []
