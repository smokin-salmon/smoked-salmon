"""What the tracker loop does once the first torrent is up: the --spectrals-after check runs once, right after
it, however many trackers follow (#567), and nothing offers to delete the folder that torrent seeds from (#568).

The runs here go against the local fake tracker of test_uploader_dry_run and a fake image host, never a real one.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    API_KEYS,
    FIRST_TORRENT_ID,
    RENAMED,
    _album,
    _lossy_with_spectrals,
    _run_up,
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

import salmon.trackers
import salmon.uploader
from salmon import cfg
from salmon.errors import RequestError
from salmon.trackers.base import BaseGazelleApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader import spectrals as spectrals_module
from salmon.uploader.seedbox import UploadManager

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

DELETE = "[d]elete music folder"
ANOTHER_TRACKER = "Would you like to upload to another tracker?"


class ThirdApi(OpsApi):
    """A third tracker, DIC by its site code, that uploads the way OPS does, so the fake tracker takes its uploads."""

    def __init__(self) -> None:
        super().__init__()
        self.site_code = "DIC"
        self.site_string = "DIC"


THREE_TRACKERS: dict[str, type[BaseGazelleApi]] = {"RED": RedApi, "OPS": OpsApi, "DIC": ThirdApi}


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


@pytest.fixture(autouse=True)
def third_tracker_key(monkeypatch) -> None:
    monkeypatch.setitem(API_KEYS, "DIC", "dic-api-key")


def _trackers(monkeypatch, *codes: str) -> None:
    monkeypatch.setattr(salmon.trackers, "tracker_list", list(codes))


def _checks(monkeypatch) -> list[tuple[str, int]]:
    """Record the (tracker, torrent ID) of each post-upload spectral check, which runs as it does in salmon."""
    checks: list[tuple[str, int]] = []
    real = spectrals_module.post_upload_spectral_check

    async def recording(gazelle_site, path, torrent_id, *args, **kwargs) -> Any:
        checks.append((gazelle_site.site_code, torrent_id))
        return await real(gazelle_site, path, torrent_id, *args, **kwargs)

    monkeypatch.setattr(salmon.uploader, "post_upload_spectral_check", recording)
    # The check's spectrals, as test_uploader_dry_run makes them: a lossy master, track 1's spectrals.
    monkeypatch.setattr(spectrals_module, "check_spectrals", _lossy_with_spectrals)
    return checks


def _description_edits(monkeypatch) -> list[tuple[str, int, str]]:
    """Record the (tracker, torrent ID, text) of each description edit, instead of sending it."""
    edits: list[tuple[str, int, str]] = []

    async def append(self, torrent_id: int, text: str) -> None:
        edits.append((self.site_code, torrent_id, text))

    monkeypatch.setattr(BaseGazelleApi, "append_to_torrent_description", append)
    return edits


def _seedbox_runs(monkeypatch) -> list[bool]:
    runs: list[bool] = []

    async def execute_upload(_self) -> None:
        runs.append(True)

    monkeypatch.setattr(UploadManager, "execute_upload", execute_upload)
    return runs


def _uploads(run) -> list[dict[str, Any]]:
    return [dict(sent.fields) for sent in run.tracker.sent if sent.query.get("action") == "upload"]


def _reports(run) -> list[str]:
    return [dict(sent.fields)["extra"] for sent in run.tracker.sent if sent.path == "/reportsv2.php"]


def _left_behind(downloads: Path) -> list[Path]:
    return list((downloads / RENAMED).rglob("*.png"))


# The --spectrals-after check (#567)


def test_with_one_tracker_the_check_runs_once_and_edits_that_torrents_description(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    _trackers(monkeypatch, "RED")
    checks, edits = _checks(monkeypatch), _description_edits(monkeypatch)

    # A new group on RED, then no comment for the lossy master report. No other tracker to offer.
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, args=("-a",), input="\n\n")

    assert run.result.exit_code == 0, run.result.output
    assert checks == [("RED", FIRST_TORRENT_ID)]
    assert len(edits) == 1
    site, torrent_id, text = edits[0]
    assert (site, torrent_id) == ("RED", FIRST_TORRENT_ID)
    # Track 1's full and zoomed spectrals, as the fake image host names them.
    spectral_urls = re.findall(r"https://images\.test/\d+\.png", text)
    assert len(spectral_urls) == 2
    # The FLAC went up before the check; its transcodes, after it, are reported as lossy masters too.
    reports = _reports(run)
    assert len(_uploads(run)) == 3 and len(reports) == 3
    assert all(all(url in report for url in spectral_urls) for report in reports)
    assert ANOTHER_TRACKER not in run.result.output
    assert _left_behind(downloads) == []


def test_without_multi_tracker_upload_the_check_runs_once(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS", "DIC")
    checks, edits = _checks(monkeypatch), _description_edits(monkeypatch)

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        args=("-a",),
        input="\n\n",
        classes=THREE_TRACKERS,
        multi_tracker_upload=False,
    )

    assert run.result.exit_code == 0, run.result.output
    assert checks == [("RED", FIRST_TORRENT_ID)]
    assert [(site, torrent_id) for site, torrent_id, _text in edits] == [("RED", FIRST_TORRENT_ID)]
    assert ANOTHER_TRACKER not in run.result.output


def test_declining_another_tracker_runs_the_check_once_before_the_first_trackers_transcodes(
    monkeypatch, tmp_path, dirs
) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS", "DIC")
    checks, edits = _checks(monkeypatch), _description_edits(monkeypatch)

    run = _run_up(
        monkeypatch, _album(tmp_path / "Album"), torrents, args=("-a",), input="\n\nn\n", classes=THREE_TRACKERS
    )

    assert run.result.exit_code == 0, run.result.output
    assert checks == [("RED", FIRST_TORRENT_ID)]
    assert len(edits) == 1
    output = run.result.output
    assert output.index("comment for the lossy approval report") < output.index("Selected formats for downconversion")
    assert output.count(ANOTHER_TRACKER) == 1
    assert len(_uploads(run)) == len(_reports(run)) == 3


def test_when_the_first_upload_fails_the_next_trackers_upload_gets_the_check(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    checks, edits = _checks(monkeypatch), _description_edits(monkeypatch)
    upload_and_report = salmon.uploader.upload_and_report

    async def red_fails(gazelle_site, *args, **kwargs) -> Any:
        if gazelle_site.site_code == "RED":
            raise RequestError("RED is down")
        return await upload_and_report(gazelle_site, *args, **kwargs)

    # A new group on RED, whose upload fails; OPS, a new group there, then no lossy master comment.
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        args=("-a",),
        input="\nOPS\n\n\n",
        upload_and_report=red_fails,
    )

    assert run.result.exit_code == 0, run.result.output
    assert "Upload to RED failed: RED is down" in run.result.output
    assert checks == [("OPS", FIRST_TORRENT_ID)]
    assert [(site, torrent_id) for site, torrent_id, _text in edits] == [("OPS", FIRST_TORRENT_ID)]


# Never offering to delete the uploaded folder (#568)


def test_the_next_trackers_group_prompts_do_not_offer_to_delete_the_folder(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")

    # RED: a new group. OPS: "d" is no answer, group 55, "d" is no answer at its confirmation either, then yes.
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\nOPS\nd\n55\nd\ny\n")

    assert run.result.exit_code == 0, run.result.output
    output = run.result.output
    ops_part = output[output.index(ANOTHER_TRACKER) :]
    # Offered before the first upload, as before; never after it.
    assert DELETE in output[: output.index(ANOTHER_TRACKER)]
    assert DELETE not in ops_part
    assert ops_part.count("Would you like to upload to an existing group?") == 2
    assert ops_part.count("Are you sure you would you like to upload this torrent to this group?") == 2
    assert [upload.get("groupid") for upload in _uploads(run)][3:] == ["55", "55", "55"]
    assert (downloads / RENAMED).is_dir()


def test_aborting_at_the_next_trackers_group_prompt_ends_the_run_and_still_seeds(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    seedbox_runs = _seedbox_runs(monkeypatch)

    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\nOPS\na\n")

    assert run.result.exit_code == 0, run.result.output
    assert run.result.exception is None
    output = run.result.output
    assert "Aborting: nothing more is uploaded. Already uploaded:" in output
    # The FLAC and its two transcodes, all on RED.
    assert output.count("/torrents.php?torrentid=", output.index("Aborting: nothing more")) == 3
    assert len(_uploads(run)) == 3
    assert seedbox_runs == [True]
    assert len(run.queued) == 6


def test_the_check_after_the_upload_does_not_offer_to_delete_the_folder_and_abort_ends_the_run(
    monkeypatch, tmp_path, dirs
) -> None:
    downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    seedbox_runs = _seedbox_runs(monkeypatch)
    checks: list[str] = []
    real = spectrals_module.post_upload_spectral_check

    async def recording(gazelle_site, *args, **kwargs) -> Any:
        checks.append(gazelle_site.site_code)
        return await real(gazelle_site, *args, **kwargs)

    async def generate_spectrals_all(path: str, spectrals_path: str, _audio_info: Any) -> dict[int, str]:
        for name in ("01 Full.png", "01 Zoom.png"):
            (Path(spectrals_path) / name).write_bytes(b"png")
        return {1: "01. one.flac"}

    async def no_viewer(*_args: Any) -> None:
        pass

    monkeypatch.setattr(salmon.uploader, "post_upload_spectral_check", recording)
    # The real check and its lossy master prompt, on spectrals made up without sox.
    monkeypatch.setattr(spectrals_module, "generate_spectrals_all", generate_spectrals_all)
    monkeypatch.setattr(spectrals_module, "view_spectrals", no_viewer)

    # RED: a new group. The lossy master prompt: "d" is no answer, then abort.
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, args=("-a",), input="\nd\na\n")

    assert run.result.exit_code == 0, run.result.output
    assert run.result.exception is None
    output = run.result.output
    assert checks == ["RED"]
    lossy_prompt = output[output.index("Checking lossy master") :]
    assert lossy_prompt.count("Is this release lossy mastered?") == 2
    assert DELETE not in lossy_prompt
    assert "Aborting: nothing more is uploaded. Already uploaded:" in output
    assert f"/torrents.php?torrentid={FIRST_TORRENT_ID}" in output[output.index("Aborting: nothing more") :]
    # Neither the transcodes nor another tracker.
    assert len(_uploads(run)) == 1
    assert ANOTHER_TRACKER not in output
    assert seedbox_runs == [True]
    assert len(run.queued) == 2
    assert (downloads / RENAMED).is_dir()
    assert _left_behind(downloads) == []
