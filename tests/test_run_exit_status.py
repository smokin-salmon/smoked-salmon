"""What `salmon up` exits with: 0, all it set out to upload went up; 1, no tracker can have taken one; 3, a tracker may
have taken one, or refused one.

run.main works the code out once the command has ended, so only a test through main sees it: a CliRunner test of `up`
still reads 0 after a refused upload. These run through main, against the local fake tracker of
helpers.uploader_dry_run, from a folder of their own with remove_source_dir on. "kept" is that folder, which goes or
stays as it did before these codes.

Each row of AS_BEFORE exits as master does; each row of CHANGED exited with something else.
"""

import io
import itertools
import signal
import socket
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from anyio.from_thread import start_blocking_portal
from helpers.library_dirs import RENAMED
from helpers.uploader_dry_run import (
    API_KEYS,
    SOURCE_URL,
    FakeTracker,
    Sent,
    _album,
    _returning_async,
    _transcode,
    fake_upload_world,
)
from tenacity import wait_none
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see images)
)
from test_uploader_run_trackers import RefusingTracker
from torf import Torrent, WriteError

import salmon.run
import salmon.trackers
from salmon import cfg
from salmon.common import commandgroup
from salmon.config.validations import Seedbox
from salmon.errors import (
    AbortAndDeleteFolder,
    EditedLogError,
    ImageUploadFailed,
    LoginError,
    RequestFailedError,
    ScrapeError,
    TLSCertificateError,
    UnknownOutcomeError,
    UploadError,
)
from salmon.tagger.audio_info import gather_audio_info
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi
from salmon.trackers.ops import OpsApi
from salmon.uploader import record, seedbox
from salmon.uploader.seedbox import UploadManager

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

TLS = TLSCertificateError("tracker.example", "certificate has expired", "OPS")
TWO = ("RED", "OPS")
KEPT_NOTHING_UP = "is kept: nothing was uploaded."
REMOVED = "Removed the source folder"
SEEDBOX_FAILED = "failed; see above"
# fake_upload_world replaces it.
QUEUE_A_SEEDBOX_TASK = UploadManager.add_upload_task
STOPPED = "Aborting upload..."


@pytest.fixture(autouse=True)
def no_waits(monkeypatch) -> None:
    monkeypatch.setattr(salmon.run, "show_release_notification", lambda: None)
    monkeypatch.setattr(salmon.run, "cleanup_tmp_dir", lambda: None)
    # Process-wide: what these runs learn stays out of other tests, and the other way round.
    monkeypatch.setattr(base, "_learned_secrets", set())
    monkeypatch.setattr(base, "_LOST_UPLOAD_FIRST_WAIT", 0)
    monkeypatch.setattr(base, "_LOST_UPLOAD_SECOND_WAIT", 0)
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a dot_torrents_dir, configured."""
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return downloads, torrents


# The trackers


class _Answering(FakeTracker):
    """A fake tracker that records an upload POST, read whole, and answers it `_answer`'s way, or as usual on None."""

    def _answer(self, request: web.Request) -> web.StreamResponse | None:
        return None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if request.query.get("action") == "upload" and request.method == "POST":
            await request.post()
            answer = self._answer(request)
            if answer is not None:
                self.sent.append(Sent(request.method, request.path, dict(request.query), []))
                return answer
        if request.query.get("action") == "torrent":
            # The infohash lookup after a lost answer: it finds nothing.
            return web.json_response({"status": "failure", "error": "bad hash"}, status=400)
        return await super()._handle(request)


class RefuseNth(_Answering):
    """Refuses the upload POSTs whose count, from 1, is in `refused`."""

    def __init__(self, *refused: int) -> None:
        super().__init__()
        self._refused = refused
        self._count = 0

    def _answer(self, request: web.Request) -> web.StreamResponse | None:
        self._count += 1
        if self._count in self._refused:
            return web.json_response({"status": "failure", "error": "Your upload was refused."})
        return None


class Lost(_Answering):
    """Answers 502 to the upload POSTs of one site, told apart by its API key, or of all."""

    def __init__(self, site: str | None = None) -> None:
        super().__init__()
        self._key = API_KEYS[site] if site else None

    def _answer(self, request: web.Request) -> web.StreamResponse | None:
        if self._key is None or request.headers.get("Authorization") == self._key:
            return web.Response(status=502, text="Bad gateway")
        return None


class LostNth(_Answering):
    """Answers 502 to the upload POST of this count, from 1."""

    def __init__(self, lost: int) -> None:
        super().__init__()
        self._lost = lost
        self._count = 0

    def _answer(self, request: web.Request) -> web.StreamResponse | None:
        self._count += 1
        return web.Response(status=502, text="Bad gateway") if self._count == self._lost else None


class Dropped(_Answering):
    """Reads the upload POST whole, then drops the connection with no answer."""

    def _answer(self, request: web.Request) -> web.StreamResponse | None:
        assert request.transport is not None
        request.transport.close()
        return web.Response()


class Status(_Answering):
    """Answers the upload POSTs with these HTTP statuses in turn, then as usual."""

    def __init__(self, *statuses: int, headers: dict[str, str] | None = None) -> None:
        super().__init__()
        self._statuses = list(statuses)
        self._headers = headers or {}

    def _answer(self, request: web.Request) -> web.StreamResponse | None:
        if not self._statuses:
            return None
        return web.Response(status=self._statuses.pop(0), text="no", headers=self._headers)


class ThenDropped(_Answering):
    """A 429 to wait out, then the connection dropped: the answer to the POST that may have got through is lost."""

    def __init__(self) -> None:
        super().__init__()
        self._answered = False

    def _answer(self, request: web.Request) -> web.StreamResponse | None:
        if not self._answered:
            self._answered = True
            return web.Response(status=429, text="slow down", headers={"Retry-After": "0"})
        assert request.transport is not None
        request.transport.close()
        return web.Response()


class Answer(_Answering):
    """Answers each upload POST with this response, as JSON, with a new torrent ID."""

    def __init__(self, **response: Any) -> None:
        super().__init__()
        self._response = response

    def _answer(self, request: web.Request) -> web.StreamResponse | None:
        torrent_id, self._next_torrent_id = self._next_torrent_id, self._next_torrent_id + 1
        return web.json_response({"status": "success", "response": {"torrentid": torrent_id, **self._response}})


class NoStatus(_Answering):
    def _answer(self, request: web.Request) -> web.StreamResponse | None:
        return web.json_response({"unexpected": True})


# What the fakes do


def _raising(error: Any) -> Callable[..., Any]:
    async def stop(*_args: Any, **_kwargs: Any) -> None:
        raise error

    return stop


def _raising_on(site_code: str, error: Any, result: Any = None) -> Callable[..., Any]:
    async def stop(gazelle_site: Any, *_args: Any, **_kwargs: Any) -> Any:
        if gazelle_site.site_code == site_code:
            raise error
        return result

    return stop


def _at(seam: str, error: Any) -> Callable[[], dict[str, Any]]:
    return lambda: {seam: _raising(error)}


def _at_sync(seam: str, error: Any) -> Callable[[], dict[str, Any]]:
    def stop(*_args: Any, **_kwargs: Any) -> None:
        raise error

    return lambda: {seam: stop}


def _at_ops(seam: str, error: Any) -> Callable[[], dict[str, Any]]:
    return lambda: {seam: _raising_on("OPS", error)}


def _cover_on(site_code: str, error: Any) -> dict[str, Any]:
    return {"resolve_cover_url": _raising_on(site_code, error, (True, None))}


def _no_cover_on(site_code: str) -> dict[str, Any]:
    async def cover(gazelle_site: Any, *_args: Any, **_kwargs: Any) -> tuple[bool, None]:
        return gazelle_site.site_code != site_code, None

    return {"resolve_cover_url": cover}


def _forbidden_on(site_code: str) -> dict[str, Any]:
    return {"_do_not_upload_refusal": lambda tracker, *_a, **_k: "listed" if tracker == site_code else None}


async def _transcode_but_v0(path: str, bitrate: str, *args: Any, **kwargs: Any) -> str:
    if bitrate == "V0":
        raise UploadError("Not overwriting existing folder.")
    return await _transcode(path, bitrate, *args, **kwargs)


# What the setups do


def _offered(monkeypatch, _album_path: Path) -> None:
    """RED is the default tracker, and OPS is offered after it."""
    monkeypatch.setattr(cfg.tracker, "default_trackers", ["RED"])
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])


def _with_a_log(_monkeypatch, album_path: Path) -> None:
    (album_path / "rip.log").write_text("not read: check_log_cambia is stubbed")


def _a_track_emptied(_monkeypatch, album_path: Path) -> None:
    (album_path / "01 - one.flac").write_bytes(b"")


def _torrents_not_written(monkeypatch, _album_path: Path) -> None:
    def write(_torrent: Torrent, path: str, **_kwargs: object) -> None:
        raise WriteError(13, path)

    monkeypatch.setattr(Torrent, "write", write)


GOOD_CLIENT = "qbittorrent+http://127.0.0.1:9"
BAD_CLIENT = "nosuchclient://x"


def _seedbox(
    *, copy_exit: int = 0, added: bool = True, clients: tuple[str, ...] = (GOOD_CLIENT,), **box: Any
) -> Callable[..., None]:
    """A real UploadManager, one box a client: rclone exits with `copy_exit`, and the client answers `added`."""

    def setup(monkeypatch, _album_path: Path) -> None:
        boxes = [
            Seedbox(name=f"box{i or ''}", type="rclone", url="box", torrent_client=client, **box)
            for i, client in enumerate(clients)
        ]
        monkeypatch.setattr(cfg, "seedbox", boxes)
        monkeypatch.setattr(UploadManager, "add_upload_task", QUEUE_A_SEEDBOX_TASK)
        monkeypatch.setattr(seedbox, "_run_rclone", _returning_async(copy_exit))
        monkeypatch.setattr(seedbox, "_add_to_downloader", _returning_async(added))

    return setup


def _one_box_of_two_fails_its_copies(monkeypatch, album_path: Path) -> None:
    _seedbox(clients=(GOOD_CLIENT, f"{GOOD_CLIENT}1"))(monkeypatch, album_path)
    copies = itertools.count()

    async def run_rclone(_commands: Any, _secrets: Any) -> int:
        return next(copies) % 2

    monkeypatch.setattr(seedbox, "_run_rclone", run_rclone)


def _a_client_add_raises(monkeypatch, album_path: Path) -> None:
    _seedbox()(monkeypatch, album_path)
    monkeypatch.setattr(seedbox, "_add_to_downloader", _raising(OSError("the torrent is gone")))


def _upload_raises(error: Exception, site: type[BaseGazelleApi] = BaseGazelleApi) -> Callable[..., None]:
    def setup(monkeypatch, _album_path: Path) -> None:
        async def upload(_self: Any, _data: Any, _files: Any) -> None:
            raise error

        monkeypatch.setattr(site, "upload", upload)

    return setup


def _closed_port() -> str:
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{free.getsockname()[1]}"


def _connection_refused_at_post(site_code: str | None = None) -> Callable[..., None]:
    """The upload's own POST goes to a closed port, for a real connection refused: the requests around it do not."""

    def setup(monkeypatch, _album_path: Path) -> None:
        real = BaseGazelleApi.upload
        closed = _closed_port()

        async def upload(self: Any, data: Any, files: Any) -> Any:
            if site_code is not None and self.site_code != site_code:
                return await real(self, data, files)
            working, self.base_url = self.base_url, closed
            try:
                return await real(self, data, files)
            finally:
                self.base_url = working

        monkeypatch.setattr(BaseGazelleApi, "upload", upload)

    return setup


def _lossy_report_raises(error: Exception) -> Callable[..., None]:
    def setup(monkeypatch, _album_path: Path) -> None:
        # OPS has one of its own.
        for site in (BaseGazelleApi, OpsApi):
            monkeypatch.setattr(site, "report_lossy_master", _raising(error))

    return setup


def _compile_files_raises(error: Exception) -> Callable[..., None]:
    def setup(monkeypatch, _album_path: Path) -> None:
        # The module, which `salmon.uploader.upload` the function hides.
        monkeypatch.setattr(sys.modules["salmon.uploader.upload"], "compile_files", _raising(error))

    return setup


# A row, and running one


@dataclass(frozen=True)
class Row:
    code: int | str  # "traceback": the error goes through main, and the interpreter exits 1
    posts: int
    said: str  # Printed on the way to this end: that the run got that far.
    kept: bool = True
    trackers: tuple[str, ...] = ("RED",)
    tracker: Callable[[], FakeTracker] = FakeTracker
    args: tuple[str, ...] = ()
    fakes: Callable[[], dict[str, Any]] = dict
    setup: Callable[..., None] | None = None
    stdin: str = "\n" * 30


def _exit_of_main() -> int | str:
    """What the process would exit with: "traceback" is an error main lets through, "interrupted" a Ctrl-C."""
    try:
        salmon.run.main()
    except SystemExit as ended:
        assert ended.code is None or isinstance(ended.code, int)
        return ended.code or 0
    except KeyboardInterrupt:
        return "interrupted"
    except (Exception, BaseExceptionGroup):
        traceback.print_exc()
        return "traceback"
    return 0


def _argv(monkeypatch, album: Path, row: Row) -> None:
    named = [arg for code in row.trackers for arg in ("-t", code)]
    monkeypatch.setattr(
        sys,
        "argv",
        ["salmon", "up", str(album), *named, "-s", "WEB", "-n", "-yyy", "--skip-integrity-check", "--skip-up"]
        + ["--source-url", SOURCE_URL, *row.args],
    )


def _main_up_apart(
    monkeypatch, capsys, album: Path, torrents: Path, row: Row, tracker: FakeTracker
) -> tuple[Any, str, str]:
    """Run `salmon up` through main against the fake tracker, on a thread: main starts an event loop of its own.

    Returns:
        What it exits with, its stdout and its stderr.
    """
    world, _queued, _logins = fake_upload_world(monkeypatch, torrents, tracker=tracker, **row.fakes())
    if row.setup:
        row.setup(monkeypatch, album)
    _argv(monkeypatch, album, row)
    monkeypatch.setattr(sys, "stdin", io.StringIO(row.stdin))

    async def run() -> Any:
        async with world.serving():
            return await anyio.to_thread.run_sync(_exit_of_main)

    code = anyio.run(run)
    captured = capsys.readouterr()
    return code, click.unstyle(captured.out), click.unstyle(captured.err)


def _main_up(monkeypatch, capsys, album: Path, torrents: Path, row: Row, tracker: FakeTracker) -> tuple[Any, str]:
    code, out, err = _main_up_apart(monkeypatch, capsys, album, torrents, row, tracker)
    return code, out + err


def _upload_posts(tracker: FakeTracker) -> int:
    return sum(sent.query.get("action") == "upload" and sent.method == "POST" for sent in tracker.sent)


def _check_row(monkeypatch, capsys, dirs: tuple[Path, Path], row: Row) -> None:
    downloads, torrents = dirs
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    album = _album(downloads.parent / "seeding" / "Album")
    tracker = row.tracker()

    code, out = _main_up(monkeypatch, capsys, album, torrents, row, tracker)

    seen = (code, _upload_posts(tracker), album.is_dir())
    assert seen == (row.code, row.posts, row.kept), out
    assert row.said in out
    if row.code == 3 and not row.kept:
        # Removed on a 3: the renamed folder still holds the album.
        flacs = list((downloads / RENAMED).glob("*.flac"))
        assert len(flacs) == 2, out


REFUSED_SAID = "Upload to RED failed: API upload failed: Your upload was refused."
OPS_REFUSED_SAID = "Upload to OPS failed: API upload failed: Your upload was refused."

AS_BEFORE = {
    # Everything went up
    "one_tracker": Row(0, 3, REMOVED, kept=False),
    "two_trackers": Row(0, 6, REMOVED, kept=False, trackers=TWO),
    "one_conversion": Row(0, 2, REMOVED, kept=False, args=("--conversions", "V0")),
    "skip_flac": Row(0, 1, "Not uploading the FLAC", args=("-g", "7", "--skip-flac-upload", "--conversions", "V0")),
    "skip_flac_nothing_asked": Row(
        0, 0, "Not uploading the FLAC", args=("-g", "7", "--skip-flac-upload", "--conversions", "none")
    ),
    "no_group_id_nothing_wanted": Row(
        0, 1, "No group id came back", kept=False, tracker=Answer, args=("--conversions", "none")
    ),
    # Asked, and declined
    "offered_then_done": Row(0, 3, "Done with this release.", kept=False, trackers=(), setup=_offered, stdin="\nn\n"),
    "offered_then_skip": Row(
        0, 3, "Skipping upload to OPS.", kept=False, trackers=(), setup=_offered, stdin="\nOPS\ns\n"
    ),
    # The torrent is up and a side step failed: the message says what to do by hand
    "fill_refused": Row(
        0,
        3,
        "Request fill failed!",
        kept=False,
        tracker=lambda: Answer(groupid=7, requestid=-1),
        args=("--request", "4242"),
    ),
    "lossy_report_refused": Row(
        0, 3, "no report", kept=False, setup=_lossy_report_raises(RequestFailedError("no report"))
    ),
    # A seedbox took it all
    "seedbox_all_good": Row(0, 3, "All upload tasks processed", kept=False, setup=_seedbox()),
    "seedbox_all_good_renamed_folder_goes": Row(
        0, 3, "(remove_local_after_copy)", kept=False, setup=_seedbox(remove_local_after_copy=True)
    ),
    "seedbox_all_good_two_trackers": Row(
        0, 6, "All upload tasks processed", kept=False, trackers=TWO, setup=_seedbox()
    ),
    # A seedbox that never configured, with no torrent for it to take
    "bad_client_disabled": Row(0, 3, REMOVED, kept=False, setup=_seedbox(clients=(BAD_CLIENT,), enabled=False)),
    "bad_client_nothing_to_do": Row(
        0,
        0,
        "Failed to configure",
        args=("-g", "7", "--skip-flac-upload", "--conversions", "none"),
        setup=_seedbox(clients=(BAD_CLIENT,)),
    ),
    "bad_client_for_another_tracker": Row(
        0, 3, "Failed to configure", kept=False, setup=_seedbox(clients=(BAD_CLIENT,), trackers=["OPS"])
    ),
    "bad_client_flac_only_and_no_flac": Row(
        0,
        1,
        "Failed to configure",
        args=("-g", "7", "--skip-flac-upload", "--conversions", "V0"),
        setup=_seedbox(clients=(BAD_CLIENT,), flac_only=True),
    ),
    "bad_client_dry_run": Row(0, 0, "Dry run: done.", args=("--dry-run",), setup=_seedbox(clients=(BAD_CLIENT,))),
    # A kind the release can't give is not a failure
    "kind_it_cannot_give": Row(0, 1, "No conversion made", kept=False, args=("--conversions", "24bit")),
    "skip_flac_kind_it_cannot_give": Row(
        0, 0, "No conversion made", args=("-g", "7", "--skip-flac-upload", "--conversions", "24bit")
    ),
    # A dry run sends nothing
    "dry_run": Row(0, 0, "Dry run: done.", args=("--dry-run",)),
    "dry_run_abort_in_the_loop": Row(
        1, 0, "Aborted!", trackers=TWO, args=("--dry-run",), fakes=lambda: _cover_on("OPS", click.Abort)
    ),
    # Stops that keep the code they had
    "usage_error_inside_the_run": Row(
        2, 0, "Error: not this way", fakes=_at("mqa_test", click.UsageError("not this way"))
    ),
    "exit_5_before_a_post": Row(5, 0, "Uploading", fakes=_at("check_requests", click.exceptions.Exit(5))),
    "system_exit_with_no_code_after_all_up": Row(
        0, 3, "Done uploading this release.", fakes=_at("remove_source_folder", SystemExit())
    ),
    "system_exit_in_a_group_before_a_post": Row(
        "traceback",
        0,
        "Exception Group Traceback",
        fakes=_at("check_requests", BaseExceptionGroup("g", [SystemExit(1)])),
    ),
    "abort_at_the_request_question": Row(1, 0, "Aborted!", fakes=_at("check_requests", click.Abort)),
    "abort_inside_upload": Row(1, 0, "Aborted!", trackers=("OPS",), setup=_upload_raises(click.Abort(), OpsApi)),
    "group_question_stdin_closed": Row(1, 0, "Aborted!", args=("-g", "7"), stdin=""),
    "rate_refused": Row(1, 0, "two.flac", fakes=lambda: {"_files_refusal": lambda *_a, **_k: True}),
    "bug_before_a_post": Row("traceback", 0, "RuntimeError: a bug", fakes=_at("check_requests", RuntimeError("a bug"))),
}


def _stop_at_ops_after_a_skip(route: str, error: Exception) -> dict[str, Any]:
    async def cover(gazelle_site: Any, *_args: Any, **_kwargs: Any) -> tuple[bool, None]:
        if gazelle_site.site_code == "OPS":
            raise error
        return route != "no_cover", None

    return {**(_forbidden_on("RED") if route == "forbidden" else {}), "resolve_cover_url": cover}


def _stops_at_ops(*, tracker: Callable[[], FakeTracker], posts: int, kept: bool = True) -> dict[str, Row]:
    """A request failure at OPS's request search, after RED's turn has gone one way or the other."""
    rows: dict[str, Row] = {}
    for name, error in (("tls", TLS), ("request_failure", RequestFailedError("the request failed"))):
        rows[f"{name}_at_the_request_search"] = Row(
            3, posts, str(error), kept=kept, trackers=TWO, tracker=tracker, fakes=_at_ops("check_requests", error)
        )
    return rows


CHANGED = {
    # Exit 1: nothing reached a tracker, and 0 said it had all gone well
    "upload_error_first_read": Row(1, 0, "unreadable", fakes=_at_sync("gather_audio_info", UploadError("unreadable"))),
    "broken_track_first_read": Row(
        1,
        0,
        "Could not read audio file: 01 - one.flac (",
        setup=_a_track_emptied,
        fakes=lambda: {"gather_audio_info": gather_audio_info},
    ),
    "torrent_cannot_be_written": Row(1, 0, "Upload to RED failed: Could not write", setup=_torrents_not_written),
    "upload_error_first_tracker": Row(
        1, 0, "There was an error: stopped", fakes=_at("resolve_cover_url", UploadError("stopped"))
    ),
    "tls_first_tracker": Row(1, 0, "salmon checkconf -t OPS", fakes=_at("recheck_dupe", TLS)),
    "request_failure_at_the_recheck": Row(
        1,
        0,
        "There was an error: the request failed",
        fakes=_at("recheck_dupe", RequestFailedError("the request failed")),
    ),
    "request_failure_first_tracker": Row(
        1, 0, KEPT_NOTHING_UP, fakes=_at("check_requests", RequestFailedError("the request failed"))
    ),
    "mqa": Row(1, 0, STOPPED, fakes=_at("mqa_test", click.Abort)),
    "you_say_no": Row(1, 0, STOPPED, fakes=_at("_review_until_confirmed", click.Abort)),
    "edited_log": Row(
        1,
        0,
        STOPPED,
        args=("-s", "CD"),
        setup=_with_a_log,
        fakes=_at("check_log_cambia", EditedLogError("Edited logs")),
    ),
    "abort_at_the_group_question": Row(1, 0, STOPPED, stdin="a\n"),
    "abort_and_delete": Row(
        1, 0, "Deleted folder", kept=False, fakes=_at("_review_until_confirmed", AbortAndDeleteFolder)
    ),
    "foreign_spectrals": Row(
        1, 0, "a foreign folder", fakes=lambda: {"foreign_spectrals_refusal": _returning_async("a foreign folder")}
    ),
    "only_tracker_forbids": Row(1, 0, KEPT_NOTHING_UP, fakes=lambda: _forbidden_on("RED")),
    "forbidden_then_done": Row(
        1, 0, "Done with this release.", trackers=(), setup=_offered, stdin="n\n", fakes=lambda: _forbidden_on("RED")
    ),
    "connection_refused_at_the_post": Row(1, 0, KEPT_NOTHING_UP, setup=_connection_refused_at_post()),
    "tls_at_the_post": Row(1, 0, KEPT_NOTHING_UP, setup=_upload_raises(TLS)),
    "too_many_requests_at_the_post": Row(
        1, 1, KEPT_NOTHING_UP, tracker=lambda: Status(429, headers={"Retry-After": "99999"})
    ),
    "dry_run_upload_error": Row(
        1,
        0,
        "stopped in a dry run",
        trackers=TWO,
        args=("--dry-run",),
        fakes=lambda: _cover_on("OPS", UploadError("stopped in a dry run")),
    ),
    "skip_flac_none_made": Row(
        1,
        0,
        "Not overwriting existing folder.",
        args=("-g", "7", "--skip-flac-upload", "--conversions", "V0"),
        fakes=_at("transcode_folder", UploadError("Not overwriting existing folder.")),
    ),
    # Not sent: a request error before the POST
    "request_error_before_a_post": Row(1, 0, "session lost", setup=_compile_files_raises(LoginError("session lost"))),
    # Not sent: a tracker skipped, then the next stops
    **{
        f"skipped_{route}_then_{name}": Row(
            1,
            0,
            f"There was an error: {error}",
            trackers=TWO,
            fakes=lambda r=route, e=error: _stop_at_ops_after_a_skip(r, e),
        )
        for route, name, error in (
            ("forbidden", "upload_error", UploadError("stopped on OPS")),
            ("no_cover", "tls", TLS),
        )
    },
    # Exit 3: refused, it reached the tracker, and a retry would send it again
    "refused": Row(3, 1, REFUSED_SAID, tracker=lambda: RefusingTracker("RED")),
    "refused_then_done": Row(
        3,
        1,
        "Done with this release.",
        trackers=(),
        setup=_offered,
        stdin="\nn\n",
        tracker=lambda: RefusingTracker("RED"),
    ),
    "refused_skip_flac": Row(
        3,
        1,
        REFUSED_SAID,
        args=("-g", "7", "--skip-flac-upload", "--conversions", "V0"),
        tracker=lambda: RefusingTracker("RED"),
    ),
    "status_401_at_the_post": Row(3, 1, "Upload to RED failed", tracker=lambda: Status(401)),
    "request_failure_inside_upload": Row(
        3, 0, "the upload page failed", setup=_upload_raises(RequestFailedError("the upload page failed"))
    ),
    # Exit 3: its outcome is unknown
    "unknown_outcome_with_no_post": Row(
        3, 0, "Upload to RED failed: lost", setup=_compile_files_raises(UnknownOutcomeError("lost"))
    ),
    "one_unknown_outcome_all_else_up": Row(
        3,
        1,
        "lost report",
        kept=False,
        args=("--conversions", "none"),
        setup=_lossy_report_raises(UnknownOutcomeError("lost report")),
    ),
    "lost": Row(3, 1, "Upload to RED failed", tracker=Lost),
    "dropped_connection": Row(3, 1, "Upload to RED failed", tracker=Dropped),
    "too_many_requests_then_dropped": Row(3, 2, "Upload to RED failed", tracker=ThenDropped),
    "lossy_report_lost": Row(
        3, 3, "lost report", kept=False, setup=_lossy_report_raises(UnknownOutcomeError("lost report"))
    ),
    "conversion_lost": Row(
        3, 2, "Upload to RED failed", kept=False, args=("--conversions", "V0"), tracker=lambda: LostNth(2)
    ),
    "red_up_ops_lost": Row(3, 4, "Upload to OPS failed", kept=False, trackers=TWO, tracker=lambda: Lost("OPS")),
    "no_status_in_the_answer": Row(3, 1, "KeyError: 'status'", tracker=NoStatus),
    "bug_raised_from_upload": Row(3, 0, "KeyError: 'status'", setup=_upload_raises(KeyError("status"))),
    # Exit 3: one tracker's turn went one way, and the next stopped or failed
    "red_up_ops_refused": Row(3, 4, OPS_REFUSED_SAID, kept=False, trackers=TWO, tracker=lambda: RefusingTracker("OPS")),
    "red_refused_ops_up": Row(3, 4, REFUSED_SAID, kept=False, trackers=TWO, tracker=lambda: RefusingTracker("RED")),
    **{f"red_up_{name}": row for name, row in _stops_at_ops(tracker=FakeTracker, posts=3, kept=False).items()},
    **{
        f"red_refused_{name}": row
        for name, row in _stops_at_ops(tracker=lambda: RefusingTracker("RED"), posts=1).items()
    },
    "red_up_ops_group_search_fails": Row(
        3,
        3,
        "Upload to OPS failed: search failed",
        kept=False,
        trackers=TWO,
        fakes=lambda: {"check_existing_group": _raising_on("OPS", RequestFailedError("search failed"))},
    ),
    "red_up_upload_error_on_ops": Row(
        3,
        3,
        "There was an error: stopped on OPS",
        trackers=TWO,
        fakes=lambda: _cover_on("OPS", UploadError("stopped on OPS")),
    ),
    "red_up_user_aborts_at_ops": Row(3, 3, "Already uploaded:", trackers=(), setup=_offered, stdin="\nOPS\na\n"),
    "red_refused_abort_at_ops": Row(
        3,
        1,
        "Aborted!",
        trackers=TWO,
        tracker=lambda: RefusingTracker("RED"),
        fakes=lambda: _cover_on("OPS", click.Abort),
    ),
    "red_up_ops_forbids": Row(3, 3, REMOVED, kept=False, trackers=TWO, fakes=lambda: _forbidden_on("OPS")),
    "red_no_cover_ops_up": Row(
        3, 3, "Skipping upload to RED.", kept=False, trackers=TWO, fakes=lambda: _no_cover_on("RED")
    ),
    "red_up_exit_1_on_ops": Row(
        3,
        3,
        "Uploading to",
        trackers=TWO,
        fakes=_at_ops("check_requests", click.exceptions.Exit(1)),
    ),
    "usage_error_after_a_post": Row(
        3, 3, "not this way", trackers=TWO, fakes=_at_ops("check_requests", click.UsageError("not this way"))
    ),
    "exit_5_after_a_post": Row(
        3, 3, "Uploading to", trackers=TWO, fakes=_at_ops("check_requests", click.exceptions.Exit(5))
    ),
    "red_up_bug_on_ops": Row(
        3,
        3,
        "RuntimeError: a bug",
        trackers=TWO,
        fakes=_at_ops("check_requests", RuntimeError("a bug")),
    ),
    "red_up_exception_group_on_ops": Row(
        3,
        3,
        "a bug in a group",
        trackers=TWO,
        fakes=_at_ops("check_requests", ExceptionGroup("g", [RuntimeError("a bug in a group")])),
    ),
    # Only a group holding more than Exceptions is a BaseExceptionGroup.
    "red_up_system_exit_in_a_group_on_ops": Row(
        3,
        3,
        "Exception Group Traceback",
        trackers=TWO,
        fakes=_at_ops("check_requests", BaseExceptionGroup("g", [SystemExit(1)])),
    ),
    # Exit 3: a conversion asked for did not go up
    "conversion_refused_the_next_not_tried": Row(
        3, 2, "Upload to RED failed", kept=False, args=("--conversions", "V0,320"), tracker=lambda: RefuseNth(2)
    ),
    "conversion_cannot_be_made": Row(
        3,
        1,
        "Not overwriting existing folder.",
        kept=False,
        args=("--conversions", "V0"),
        fakes=_at("transcode_folder", UploadError("Not overwriting existing folder.")),
    ),
    "one_conversion_of_two_cannot_be_made": Row(
        3,
        2,
        "Not overwriting existing folder.",
        kept=False,
        args=("--conversions", "V0,320"),
        fakes=lambda: {"transcode_folder": _transcode_but_v0},
    ),
    "no_group_id_conversion_wanted": Row(
        3, 1, "No group id came back", kept=False, tracker=Answer, args=("--conversions", "V0")
    ),
    "conversion_not_made_then_skip": Row(
        3,
        1,
        "Skipping upload to OPS.",
        kept=False,
        trackers=(),
        setup=_offered,
        stdin="\nOPS\ns\n",
        args=("--conversions", "V0"),
        fakes=_at("transcode_folder", UploadError("Not overwriting existing folder.")),
    ),
    "skip_flac_one_conversion_of_two": Row(
        3,
        1,
        "Not overwriting existing folder.",
        args=("-g", "7", "--skip-flac-upload", "--conversions", "V0,320"),
        fakes=lambda: {"transcode_folder": _transcode_but_v0},
    ),
    # Exit 3: the torrents are up, and a seedbox didn't get or seed them
    "seedbox_copy_fails": Row(3, 3, SEEDBOX_FAILED, kept=False, setup=_seedbox(copy_exit=1)),
    "seedbox_add_fails": Row(3, 3, SEEDBOX_FAILED, kept=False, setup=_seedbox(added=False)),
    "seedbox_copy_and_add_fail": Row(3, 3, SEEDBOX_FAILED, kept=False, setup=_seedbox(copy_exit=1, added=False)),
    "seedbox_copy_fails_two_trackers": Row(3, 6, SEEDBOX_FAILED, kept=False, trackers=TWO, setup=_seedbox(copy_exit=1)),
    "bad_client_torrent_up": Row(3, 3, "Failed to configure", kept=False, setup=_seedbox(clients=(BAD_CLIENT,))),
    "bad_client_one_box_of_two": Row(
        3, 3, "Failed to configure", kept=False, setup=_seedbox(clients=(GOOD_CLIENT, BAD_CLIENT))
    ),
    "seedbox_one_box_of_two_fails": Row(3, 3, SEEDBOX_FAILED, kept=False, setup=_one_box_of_two_fails_its_copies),
    "seedbox_client_add_raises": Row(3, 3, "the torrent is gone", kept=False, setup=_a_client_add_raises),
    "bad_client_first_good_box_still_served": Row(
        3, 3, "All upload tasks processed", kept=False, setup=_seedbox(clients=(BAD_CLIENT, GOOD_CLIENT))
    ),
    "bad_client_pinned_to_this_tracker": Row(
        3, 3, "Failed to configure", kept=False, setup=_seedbox(clients=(BAD_CLIENT,), trackers=["RED"])
    ),
    "bad_client_flac_only_flac_up": Row(
        3, 3, "Failed to configure", kept=False, setup=_seedbox(clients=(BAD_CLIENT,), flac_only=True)
    ),
    "bad_client_conversion_only": Row(
        3,
        1,
        "Failed to configure",
        args=("-g", "7", "--skip-flac-upload", "--conversions", "V0"),
        setup=_seedbox(clients=(BAD_CLIENT,)),
    ),
    "seedbox_copy_fails_renamed_folder_stays": Row(
        3, 3, "the copy to box failed", kept=False, setup=_seedbox(copy_exit=1, remove_local_after_copy=True)
    ),
    # Exit 3: after a POST, something else failed
    "bug_between_the_post_and_its_note": Row(
        3, 1, "RuntimeError: a bug in the report", setup=_lossy_report_raises(RuntimeError("a bug in the report"))
    ),
    "exit_1_after_all_up": Row(
        3, 3, "Done uploading this release.", fakes=_at("remove_source_folder", click.exceptions.Exit(1))
    ),
    "exit_5_after_all_up": Row(
        3, 3, "Done uploading this release.", fakes=_at("remove_source_folder", click.exceptions.Exit(5))
    ),
    "source_removal_raises_after_all_up": Row(
        3, 3, "permission denied", fakes=_at("remove_source_folder", OSError(13, "permission denied"))
    ),
    "spectrals_after_upload_error": Row(
        3,
        1,
        "There was an error: not replacing the folder",
        args=("--spectrals-after",),
        fakes=_at("post_upload_spectral_check", UploadError("not replacing the folder")),
    ),
}


def _keep_reason(monkeypatch, capsys, dirs: tuple[Path, Path], row: Row) -> str | None:
    """What the run tells remove_local_copies, which removes the renamed folder under remove_local_after_copy."""
    downloads, torrents = dirs
    reasons: list[str | None] = []
    monkeypatch.setattr(UploadManager, "remove_local_copies", lambda _self, because=None: reasons.append(because))
    _main_up(monkeypatch, capsys, _album(downloads.parent / "seeding" / "Album"), torrents, row, row.tracker())
    assert len(reasons) == 1
    return reasons[0]


@pytest.mark.parametrize(
    "name", ["conversion_cannot_be_made", "one_conversion_of_two_cannot_be_made", "no_group_id_conversion_wanted"]
)
def test_a_conversion_not_made_keeps_the_renamed_folder(monkeypatch, capsys, dirs, name: str) -> None:
    reason = _keep_reason(monkeypatch, capsys, dirs, CHANGED[name])

    assert reason == "the upload to RED failed or was skipped"


def test_a_failed_copy_with_no_seed_queued_counts_as_failed(monkeypatch, tmp_path) -> None:
    # A run always queues a seed with its copy, and a failed copy fails that seed too: this is the copy on its own.
    fake_upload_world(monkeypatch, tmp_path)
    _seedbox(copy_exit=1)(monkeypatch, tmp_path)
    manager = UploadManager()
    manager.add_upload_task(str(tmp_path), "folder", is_flac=True)

    with record.recording() as run:
        anyio.run(manager.execute_upload)

    assert run.seedbox_failed


def test_as_before_all_up_lets_the_renamed_folder_go(monkeypatch, capsys, dirs) -> None:
    reason = _keep_reason(monkeypatch, capsys, dirs, AS_BEFORE["one_conversion"])

    assert reason is None


@pytest.mark.parametrize("name", AS_BEFORE)
def test_as_before_up_ends(monkeypatch, capsys, dirs, name: str) -> None:
    _check_row(monkeypatch, capsys, dirs, AS_BEFORE[name])


@pytest.mark.parametrize("name", CHANGED)
def test_up_ends(monkeypatch, capsys, dirs, name: str) -> None:
    _check_row(monkeypatch, capsys, dirs, CHANGED[name])


def test_a_bug_after_a_post_still_prints_its_traceback(monkeypatch, capsys, dirs) -> None:
    row = CHANGED["red_up_bug_on_ops"]
    downloads, torrents = dirs
    album = _album(downloads / "Album")

    code, out, err = _main_up_apart(monkeypatch, capsys, album, torrents, row, FakeTracker())

    assert code == 3
    assert "Traceback (most recent call last):" in err
    assert "Traceback (most recent call last):" not in out


class _DeadPipe(io.StringIO):
    def write(self, _text: str) -> int:
        raise BrokenPipeError


def test_a_dead_stderr_after_a_post_still_exits_3(monkeypatch, capsys, dirs) -> None:
    # The traceback can't be printed. Were that to end main, the interpreter's 1 would tell a script to send it again.
    async def stop(gazelle_site: Any, *_args: Any, **_kwargs: Any) -> None:
        if gazelle_site.site_code == "OPS":
            monkeypatch.setattr(sys, "stderr", _DeadPipe())
            raise RuntimeError("a bug")

    row = Row(3, 3, "Uploading to", trackers=TWO, fakes=lambda: {"check_requests": stop})

    _check_row(monkeypatch, capsys, dirs, row)


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(Row(1, 0, "Uploading", fakes=_at("check_requests", SystemExit("stopped"))), id="before_a_post"),
        pytest.param(
            Row(3, 3, "Uploading", trackers=TWO, fakes=_at_ops("check_requests", SystemExit("stopped"))),
            id="after_a_post",
        ),
        pytest.param(
            Row(3, 3, "Done uploading this release.", fakes=_at("remove_source_folder", SystemExit("stopped"))),
            id="after_all_up",
        ),
    ],
)
def test_an_exit_with_text_for_a_code_counts_as_1(monkeypatch, capsys, dirs, row: Row) -> None:
    # The interpreter exits 1 for one. Nothing in salmon raises one today.
    _check_row(monkeypatch, capsys, dirs, row)


# A script's retry


RETRIED = {
    "refused": (CHANGED["refused"], [3], 1),
    "lost": (CHANGED["lost"], [3], 1),
    "red_up_ops_refused": (CHANGED["red_up_ops_refused"], [3], 4),
    "mqa": (CHANGED["mqa"], [1, 1, 1], 0),
    "connection_refused": (CHANGED["connection_refused_at_the_post"], [1, 1, 1], 0),
    # The one 1 that sends: the tracker turned it away unread, so sending it again is what it asks for.
    "too_many_requests": (
        Row(1, 1, "", tracker=lambda: Status(429, 429, 429, headers={"Retry-After": "99999"})),
        [1, 1, 1],
        3,
    ),
}


@pytest.mark.parametrize("name", RETRIED)
def test_a_script_retrying_only_on_1_sends_a_refused_or_lost_upload_once(monkeypatch, capsys, dirs, name: str) -> None:
    row, codes, posts = RETRIED[name]
    downloads, torrents = dirs
    album = _album(downloads / RENAMED)
    tracker = row.tracker()

    seen = []
    for _ in range(3):
        code, _out = _main_up(monkeypatch, capsys, album, torrents, row, tracker)
        seen.append(code)
        if code != 1:
            break

    sent = _upload_posts(tracker)
    assert seen == codes
    assert sent == posts


@pytest.mark.parametrize(
    ("final_name", "stdin", "passes"),
    [
        pytest.param(True, "\n" * 30, [(3, 1), (3, 1), (3, 1)], id="at_its_final_name_answering"),
        pytest.param(False, "\n" * 30, [(3, 1), (1, 0), (1, 0)], id="another_folder_answering"),
        pytest.param(True, "", [(1, 0), (1, 0), (1, 0)], id="at_its_final_name_stdin_closed"),
    ],
)
def test_a_loop_that_only_stops_at_0_goes_on_after_a_refused_upload(
    monkeypatch, capsys, dirs, final_name: bool, stdin: str, passes: list[tuple[int, int]]
) -> None:
    # What the release note warns of: a 3 doesn't end `while ! salmon up X`.
    downloads, torrents = dirs
    album = _album(downloads / RENAMED if final_name else downloads.parent / "seeding" / "Album")
    tracker = RefusingTracker("RED")
    row = Row(3, 1, "", tracker=lambda: tracker, stdin=stdin)

    seen = []
    for _ in range(3):
        before = _upload_posts(tracker)
        code, _out = _main_up(monkeypatch, capsys, album, torrents, row, tracker)
        seen.append((code, _upload_posts(tracker) - before))

    assert seen == passes


# Ctrl-C


@pytest.mark.parametrize(("site_code", "posts"), [("RED", 0), ("OPS", 3)])
def test_as_before_ctrl_c_goes_through_main(monkeypatch, capsys, dirs, site_code: str, posts: int) -> None:
    # A real SIGINT: the shell then sees 130, so `while ! salmon up` ends with it. On the test's thread, as a signal
    # handler must be, with the tracker on a thread of its own.
    downloads, torrents = dirs
    album = _album(downloads / RENAMED)

    async def interrupt(gazelle_site: Any, *_args: Any, **_kwargs: Any) -> None:
        if gazelle_site.site_code == site_code:
            signal.raise_signal(signal.SIGINT)
            await anyio.sleep(5)

    world, _queued, _logins = fake_upload_world(monkeypatch, torrents, check_requests=interrupt)
    _argv(monkeypatch, album, Row(0, 0, "", trackers=TWO))
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" * 30))
    # Whatever started pytest: a background job in a shell has SIGINT ignored.
    before = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with start_blocking_portal() as portal, portal.wrap_async_context_manager(world.serving()):
            code = _exit_of_main()
    finally:
        signal.signal(signal.SIGINT, before)
    capsys.readouterr()

    sent = _upload_posts(world)
    assert code == "interrupted"
    assert sent == posts


# Help


def test_up_help_says_what_it_exits_with(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["salmon", "up", "--help"])

    code = _exit_of_main()

    assert code == 0
    words = " ".join(capsys.readouterr().out.split())
    assert "Exits with 0 when everything the run set out to upload went up" in words
    assert "1 when no tracker can have taken an upload" in words
    assert "2 on a usage error" in words
    assert "3 when a tracker may have taken one, or refused one" in words
    assert "a tracker you skipped at its group question, or did not pick when offered, counts as done" in words
    assert "a retry is safe, keep the album" in words
    assert "a torrent is up, an upload was refused or an outcome is unknown" in words
    assert "Don't retry the whole run, and don't delete the album" in words
    assert "A failed seedbox copy or client add is a 3" in words


# Other commands


def _main_on_a_command(monkeypatch, capsys, command: Callable[[], Any]) -> tuple[Any, str]:
    """Run main with a command added to the real command group, which does what `command` does."""

    @click.command()
    async def probe() -> None:
        click.echo("the command ran")
        command()

    monkeypatch.setitem(commandgroup.commands, "probe", probe)
    monkeypatch.setattr(sys, "argv", ["salmon", "probe"])
    code = _exit_of_main()
    captured = capsys.readouterr()
    out = click.unstyle(captured.out + captured.err)
    assert "the command ran" in out
    return code, out


def _raises(error: BaseException) -> Callable[[], None]:
    def command() -> None:
        raise error

    return command


@pytest.mark.parametrize(
    ("error", "said"),
    [
        pytest.param(UploadError("no room"), ["There was an error: no room"], id="UploadError"),
        pytest.param(
            TLS,
            ["There was an error: TLS certificate verification failed", "salmon checkconf -t OPS"],
            id="TLSCertificateError",
        ),
        pytest.param(
            RequestFailedError("the tracker said no"), ["There was an error: the tracker said no"], id="RequestError"
        ),
        pytest.param(ImageUploadFailed("no host"), ["There was an error: no host"], id="ImageUploadFailed"),
        pytest.param(ScrapeError("no store"), ["There was an error: no store"], id="ScrapeError"),
        pytest.param(ImportError("no sox"), ["You are missing required dependencies: no sox"], id="ImportError"),
    ],
)
def test_any_command_exits_1_after_an_error_main_prints(monkeypatch, capsys, error: Exception, said: list[str]) -> None:
    code, out = _main_on_a_command(monkeypatch, capsys, _raises(error))

    assert code == 1
    for words in said:
        assert words in out


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(click.exceptions.Exit(1), 1, id="Exit_1"),
        pytest.param(RuntimeError("a bug"), "traceback", id="a_bug"),
    ],
)
def test_as_before_another_command_ends_with_what_it_raises(monkeypatch, capsys, error: Exception, expected) -> None:
    code, _out = _main_on_a_command(monkeypatch, capsys, _raises(error))

    assert code == expected


def test_as_before_another_command_that_ends_well_exits_0(monkeypatch, capsys) -> None:
    code, _out = _main_on_a_command(monkeypatch, capsys, lambda: None)

    assert code == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(None, 0, id="ends_well"),
        pytest.param(click.exceptions.Exit(1), 1, id="Exit_1"),
        pytest.param(RuntimeError("a bug"), "traceback", id="a_bug"),
    ],
)
def test_as_before_the_codes_are_ups_alone_whatever_the_run_recorded(monkeypatch, capsys, error, expected) -> None:
    # cross-upload notes into the same record, and is not `salmon up`: 3 is not its code.
    def command() -> None:
        record.note_upload("RED", "FLAC", "https://tracker.example/torrents.php?torrentid=1")
        record.note_unknown_outcome(UnknownOutcomeError("the answer was lost"))
        if error is not None:
            raise error

    code, _out = _main_on_a_command(monkeypatch, capsys, command)

    assert code == expected
