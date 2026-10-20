"""What an upload run keeps from one tracker to the next (#773): a request ID is only for the tracker it was
given or found for, and the path limit is the one of the trackers the run reaches.

The runs go against the local fake tracker of test_uploader_dry_run, never a real one.
"""

from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    API_KEYS,
    FakeTracker,
    Run,
    Sent,
    _album,
    _returning_async,
    _run_up,
    image_uploads,  # noqa: F401 (a fixture: see images)
)

import salmon.trackers
import salmon.uploader as uploader
from salmon import cfg
from salmon.trackers.ops import OpsApi

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")


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


class RefusingTracker(FakeTracker):
    """A fake tracker whose upload for one site, told apart by its API key, fails with an error."""

    def __init__(self, refused: str) -> None:
        super().__init__()
        self._refused = API_KEYS[refused]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if request.query.get("action") == "upload" and request.headers.get("Authorization") == self._refused:
            await request.post()
            self.sent.append(Sent(request.method, request.path, dict(request.query), []))
            return web.json_response({"status": "failure", "error": "Your upload was refused."})
        return await super()._handle(request)


def _uploads(run: Run) -> list[dict[str, Any]]:
    return [dict(sent.fields) for sent in run.tracker.sent if sent.query.get("action") == "upload" and sent.fields]


def _assert_ops_got_no_request_4242(run: Run) -> None:
    assert run.result.exit_code == 0, run.result.output
    assert "Not filling request 4242: it is RED's." in run.result.output
    ops_uploads = _uploads(run)
    assert ops_uploads, run.result.output
    assert all(upload.get("requestid") != "4242" for upload in ops_uploads)
    # OPS's own requests are searched instead.
    assert "requests" in {sent.query.get("action") for sent in run.tracker.sent}


def test_the_request_of_a_tracker_whose_upload_failed_is_not_sent_to_the_next(monkeypatch, dirs) -> None:
    downloads, torrents = dirs
    monkeypatch.setattr(cfg.tracker, "default_trackers", ["RED", "OPS"])

    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        args=("--request", "4242"),
        trackers=(),
        tracker=RefusingTracker("RED"),
    )

    assert "Upload to RED failed: API upload failed: Your upload was refused." in run.result.output
    _assert_ops_got_no_request_4242(run)


def test_the_request_of_a_tracker_whose_cover_check_refused_is_not_sent_to_the_next(monkeypatch, dirs) -> None:
    downloads, torrents = dirs
    monkeypatch.setattr(cfg.tracker, "default_trackers", ["RED", "OPS"])

    async def refusing_red(gazelle_site, *_args: Any, **_kwargs: Any) -> tuple[bool, None]:
        return gazelle_site.site_code != "RED", None

    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        args=("--request", "4242"),
        trackers=(),
        resolve_cover_url=refusing_red,
    )

    assert "Skipping upload to RED." in run.result.output
    _assert_ops_got_no_request_4242(run)


def test_with_skip_flac_upload_the_path_limit_is_the_first_trackers_alone(monkeypatch, dirs) -> None:
    downloads, torrents = dirs
    monkeypatch.setattr(cfg.tracker, "default_trackers", ["OPS"])
    limits: list[int] = []

    async def recording(_path: str, _scene: bool, *, max_path_length: int, **_kwargs: Any) -> None:
        limits.append(max_path_length)

    # OPS (255) is the default tracker; RED (180) is configured and would be offered after it with
    # multi_tracker_upload, but --skip-flac-upload stops the run after OPS, which takes the transcodes into its group.
    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        args=("-g", "55", "--skip-flac-upload"),
        input="y\n",
        trackers=(),
        check_spectrals=_returning_async((False, None)),
        check_folder_structure=recording,
    )

    assert run.result.exit_code == 0, run.result.output
    assert limits
    assert set(limits) == {255}


@pytest.mark.parametrize(
    ("multi_tracker_upload", "flac_group", "expected"),
    [
        (True, None, 180),  # RED is offered after OPS: the strictest of both
        (False, None, 255),  # the run stops after OPS
        (True, {"id": 55}, 255),  # --skip-flac-upload: the run stops after OPS
    ],
)
def test_the_path_limit_of_a_run_without_named_trackers(
    monkeypatch, multi_tracker_upload, flac_group, expected
) -> None:
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", multi_tracker_upload)
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])

    run_trackers = uploader._trackers_for_run("OPS", flac_group, None)

    assert uploader._max_path_length_for_run(OpsApi(), run_trackers) == expected
