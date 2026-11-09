"""A tracker, store or image host that fails where no step catches it ends the command with a reason, not a traceback.

Through run.main, against the local fake tracker of helpers.uploader_dry_run. What each ends with is what #949 set.
"""

import io
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from helpers.uploader_dry_run import API_KEYS, FakeTracker, _album, fake_upload_world
from test_run_exit_status import (
    TLS,
    TWO,
    Row,
    _at,
    _at_ops,
    _cover_on,
    _exit_of_main,
    _main_up_apart,
    _upload_posts,
    _upload_raises,
    dirs,  # noqa: F401 (a fixture)
    no_waits,  # noqa: F401 (a fixture, autouse)
)
from test_uploader_dry_run import image_uploads  # noqa: F401 (a fixture: see images)

import salmon.images
import salmon.trackers
from salmon.errors import ImageUploadFailed, RequestFailedError
from salmon.uploader import dupe_checker

pytestmark = pytest.mark.usefixtures("image_uploads")

NO_CONVERSIONS = ("--conversions", "none")
TRACEBACK = "Traceback (most recent call last)"


class FailingOn(FakeTracker):
    """Answers `status` to GETs of ajax.php's `action`, from one tracker (by its API key) or all of them."""

    def __init__(self, action: str, status: int = 502, site: str | None = None) -> None:
        super().__init__()
        self.action, self.status, self.key = action, status, API_KEYS[site] if site else None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        hit = request.query.get("action") == self.action and (
            self.key is None or request.headers.get("Authorization") == self.key
        )
        if hit:
            if self.status == 302:
                raise web.HTTPFound("/login.php")
            return web.Response(status=self.status, text="Bad gateway")
        return await super()._handle(request)


def _up(monkeypatch, capsys, dirs: tuple[Path, Path], tracker: FakeTracker, **row: Any) -> tuple[Any, str]:  # noqa: F811
    downloads, torrents = dirs
    album = _album(downloads.parent / "seeding" / "Album")
    row = {"args": NO_CONVERSIONS, **row}
    code, out, err = _main_up_apart(monkeypatch, capsys, album, torrents, Row(0, 0, "", **row), tracker)
    return code, out + err


def _main(monkeypatch, capsys, *args: str) -> tuple[Any, str]:
    monkeypatch.setattr(sys, "argv", ["salmon", *args])
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" * 30))
    code = _exit_of_main()
    captured = capsys.readouterr()
    return code, click.unstyle(captured.out + captured.err)


# The request search, inside the loop's try


@pytest.mark.parametrize(
    ("status", "said"),
    [
        pytest.param(502, "Upload to RED failed: Server error 502", id="502"),
        pytest.param(302, "Upload to RED failed: RED redirected to its login page", id="expired_session"),
    ],
)
def test_request_search_failing_on_the_first_tracker(monkeypatch, capsys, dirs, status: int, said: str) -> None:  # noqa: F811
    tracker = FailingOn("requests", status=status)

    code, out = _up(monkeypatch, capsys, dirs, tracker)

    assert TRACEBACK not in out, out
    assert said in out, out
    assert (code, _upload_posts(tracker)) == (1, 0), out


def test_request_search_failing_on_the_first_tracker_goes_on_to_the_next(monkeypatch, capsys, dirs) -> None:  # noqa: F811
    tracker = FailingOn("requests", site="RED")

    code, out = _up(monkeypatch, capsys, dirs, tracker, trackers=TWO)

    assert TRACEBACK not in out, out
    assert "Upload to RED failed: " in out, out
    assert "Upload to OPS failed" not in out, out
    assert (code, _upload_posts(tracker)) == (3, 1), out


def test_request_search_failing_on_a_later_tracker(monkeypatch, capsys, dirs) -> None:  # noqa: F811
    tracker = FailingOn("requests", site="OPS")

    code, out = _up(monkeypatch, capsys, dirs, tracker, trackers=TWO)

    assert TRACEBACK not in out, out
    assert "Upload to OPS failed: Server error 502" in out, out
    assert (code, _upload_posts(tracker)) == (3, 1), out


# A failure at the certificate: the same hint wherever it ends a tracker's turn


CHECKCONF_HINT = (
    "Your session cookie and API key are not the cause. Run salmon checkconf -t OPS to see where"
    " Python gets its CA certificates."
)


def _group_search_on_red_raises(error: Exception) -> Callable[..., None]:
    def setup(monkeypatch, _album_path: Path) -> None:
        async def search(gazelle_site: Any, *_args: Any, **_kwargs: Any) -> Any:
            if gazelle_site.site_code == "RED":
                raise error
            return [], None

        monkeypatch.setattr(dupe_checker, "fetch_existing_group_candidates", search)

    return setup


# What the run says before the hint, what a certificate failure exits with, and how to make it fail there.
ENDS_OF_A_TURN: dict[str, tuple[str, int, Callable[[Exception], dict[str, Any]]]] = {
    "request_search": ("Upload to RED failed: {}", 1, lambda e: {"fakes": _at("check_requests", e)}),
    "upload_post": ("Upload to RED failed: {}", 1, lambda e: {"setup": _upload_raises(e)}),
    "group_search_on_a_later_tracker": (
        "Upload to OPS failed: {}",
        3,
        lambda e: {"trackers": TWO, "fakes": _at_ops("check_existing_group", e)},
    ),
    "first_group_search_with_none_to_follow": (
        "Could not search RED for dupes: {}",
        1,
        lambda e: {"setup": _group_search_on_red_raises(e)},
    ),
    "first_group_search_with_one_to_follow": (
        "Could not search RED for dupes ({}): skipping it.",
        3,
        lambda e: {"trackers": TWO, "setup": _group_search_on_red_raises(e)},
    ),
    "only_main_catches_it": ("There was an error: {}", 1, lambda e: {"fakes": _at("recheck_dupe", e)}),
}


@pytest.mark.parametrize("place", ENDS_OF_A_TURN)
def test_a_certificate_failure_prints_the_hint_once_after_its_reason(monkeypatch, capsys, dirs, place: str) -> None:  # noqa: F811
    said, exits, failing = ENDS_OF_A_TURN[place]

    code, out = _up(monkeypatch, capsys, dirs, FakeTracker(), **failing(TLS))

    assert TRACEBACK not in out, out
    assert code == exits, out
    assert out.count(CHECKCONF_HINT) == 1, out
    assert out.index(said.format(TLS)) < out.index(CHECKCONF_HINT), out


@pytest.mark.parametrize("place", ENDS_OF_A_TURN)
def test_as_before_any_other_failure_prints_no_hint(monkeypatch, capsys, dirs, place: str) -> None:  # noqa: F811
    said, _exits, failing = ENDS_OF_A_TURN[place]
    error = RequestFailedError("the tracker said no")

    _code, out = _up(monkeypatch, capsys, dirs, FakeTracker(), **failing(error))

    assert TRACEBACK not in out, out
    assert said.format(error) in out, out
    assert "checkconf" not in out, out


# What only run.main catches


def test_a_request_error_only_main_catches_after_a_post_exits_3(monkeypatch, capsys, dirs) -> None:  # noqa: F811
    # OPS's cover, a step of the loop outside its try: RED's torrent is up, so a script must not send it again.
    error = RequestFailedError("the cover failed")
    tracker = FakeTracker()

    code, out = _up(monkeypatch, capsys, dirs, tracker, trackers=TWO, fakes=lambda: _cover_on("OPS", error))

    assert TRACEBACK not in out, out
    assert "There was an error: the cover failed" in out, out
    assert (code, _upload_posts(tracker)) == (3, 1), out


def test_checkspecs_of_a_torrent_the_tracker_does_not_have(monkeypatch, capsys, dirs, tmp_path) -> None:  # noqa: F811
    class NoSuchTorrent(FakeTracker):
        async def _handle(self, request: web.Request) -> web.StreamResponse:
            if request.query.get("action") == "torrent":
                return web.json_response({"status": "failure", "error": "bad id parameter"}, status=400)
            return await super()._handle(request)

    _downloads, torrents = dirs
    tracker, _queued, _logins = fake_upload_world(monkeypatch, torrents, tracker=NoSuchTorrent())
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])

    async def run() -> tuple[Any, str]:
        async with tracker.serving():
            return await anyio.to_thread.run_sync(
                _main, monkeypatch, capsys, "checkspecs", "-t", "RED", "-i", "999", str(tmp_path)
            )

    code, out = anyio.run(run)

    assert TRACEBACK not in out, out
    assert "There was an error: " in out, out
    assert "bad id parameter" in out, out
    assert code == 1, out


def test_images_up_when_the_host_fails(monkeypatch, capsys, tmp_path) -> None:
    class Failing:
        host = "ptpimg"

        @asynccontextmanager
        async def connections(self, _limit: int) -> AsyncIterator[None]:
            yield

        async def upload_file(self, _filename: str) -> tuple[str, None]:
            raise ImageUploadFailed("the host answered 503")

    for name in list(salmon.images.HOSTS):
        monkeypatch.setitem(salmon.images.HOSTS, name, SimpleNamespace(ImageUploader=Failing))
    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG")

    code, out = _main(monkeypatch, capsys, "images", "up", str(image))

    assert TRACEBACK not in out, out
    assert "Image Upload Failed. the host answered 503" in out, out
    assert "There was an error: Failed to upload image" in out, out
    assert code == 1, out


# As before


def test_as_before_nothing_failing_sends_what_it_sent(monkeypatch, capsys, dirs) -> None:  # noqa: F811
    tracker = FakeTracker()

    code, out = _up(monkeypatch, capsys, dirs, tracker, trackers=TWO)

    steps = [
        (sent.method, sent.query.get("action"))
        for sent in tracker.sent
        if sent.query.get("action") in ("requests", "upload")
    ]
    assert steps == [("GET", "requests"), ("POST", "upload")] * 2, out
    assert code == 0, out
