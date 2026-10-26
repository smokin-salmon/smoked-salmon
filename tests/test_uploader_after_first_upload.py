"""What the tracker loop does once the first torrent is up: the --spectrals-after check runs once, right after
it, however many trackers follow (#567), and nothing offers to delete the folder that torrent seeds from (#568).

The runs here go against the local fake tracker of test_uploader_dry_run and a fake image host, never a real one.
"""

import errno
import os
import re
from pathlib import Path
from typing import Any

import pyperclip
import pytest
from aiohttp import web
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    API_KEYS,
    FIRST_TORRENT_ID,
    RENAMED,
    FakeTracker,
    _album,
    _lossy_with_spectrals,
    _run_up,
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)
from torf import Torrent, WriteError

import salmon.trackers
import salmon.uploader
from salmon import cfg, interaction
from salmon.errors import RequestError, UnknownOutcomeError
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


def test_a_refused_lossy_master_report_is_not_a_failed_upload(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    checks, edits = _checks(monkeypatch), _description_edits(monkeypatch)
    refused: list[int] = []

    async def report_lossy_master(_self, torrent_id: int, _comment: str, _source: str) -> bool:
        refused.append(torrent_id)
        raise RequestError("Failed to report the torrent for lossy master, code 500.")

    for cls in (BaseGazelleApi, OpsApi):
        monkeypatch.setattr(cls, "report_lossy_master", report_lossy_master)

    # RED: a new group, no lossy master comment. OPS: a new group.
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, args=("-a",), input="\n\nOPS\n\n")

    assert run.result.exit_code == 0, run.result.output
    output = run.result.output
    assert "Upload to RED failed" not in output and "Upload to OPS failed" not in output
    # The check's report, then the transcodes' on RED; then OPS's three, which carry the check's result too.
    assert checks == [("RED", FIRST_TORRENT_ID)]
    assert len(edits) == 1
    assert refused == list(range(FIRST_TORRENT_ID, FIRST_TORRENT_ID + 6)), output
    assert len(_uploads(run)) == 6
    # Each refused report is printed, to be filed by hand, with the spectral links.
    assert output.count("did not take the lossy master report") == 6
    assert output.count("[hide=Spectrals]") >= 6


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


def test_aborting_after_an_upload_keeps_the_source_folder(monkeypatch, tmp_path, dirs) -> None:
    # remove_source_dir waits for the run to finish: an abort at the next tracker is not one, even with uploads.
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    _seedbox_runs(monkeypatch)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    album = _album(tmp_path / "Album")

    run = _run_up(monkeypatch, album, torrents, input="\nOPS\na\n")

    assert run.result.exit_code == 0, run.result.output
    assert "Aborting: nothing more is uploaded. Already uploaded:" in run.result.output
    assert f"remove_source_dir is on, but {album} is kept: the run did not finish." in run.result.output
    assert album.is_dir()


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
    monkeypatch.setattr(interaction, "show_spectrals", no_viewer)

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


def test_the_check_after_the_upload_leaves_the_albums_own_spectrals_folder(monkeypatch, tmp_path, dirs) -> None:
    # The torrent seeds from the renamed copy: the check used to empty its Spectrals folder, then delete it.
    downloads, torrents = dirs
    _trackers(monkeypatch, "RED")
    checks, edits = _checks(monkeypatch), _description_edits(monkeypatch)
    album = _album(tmp_path / "Album")
    (album / "Spectrals").mkdir()
    (album / "Spectrals" / "notes.txt").write_bytes(b"mine")

    run = _run_up(monkeypatch, album, torrents, args=("-a",), input="\n\n")

    assert run.result.exit_code == 0, run.result.output
    assert checks == [("RED", FIRST_TORRENT_ID)]
    assert len(edits) == 1
    assert (album / "Spectrals" / "notes.txt").read_bytes() == b"mine"
    assert (downloads / RENAMED / "Spectrals" / "notes.txt").read_bytes() == b"mine"
    assert _left_behind(downloads) == []
    assert list(downloads.glob("spectrals_*")) == []


# Once the tracker has the torrent: what fails after it does not report it failed, and it is seeded


class NoGroupIdTracker(FakeTracker):
    """Answers an upload with group 0, as an API answer may."""

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        response = await super()._handle(request)
        if request.path == "/ajax.php" and request.query.get("action") == "upload":
            # The torrent id the fake tracker just gave it.
            torrent_id = self._next_torrent_id - 1
            return web.json_response({"status": "success", "response": {"torrentid": torrent_id, "groupid": 0}})
        return response


def test_an_upload_whose_group_id_does_not_come_back_offers_no_conversions(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED")
    seedbox_runs = _seedbox_runs(monkeypatch)
    offered: list[bool] = []
    choose = salmon.uploader.prompt_downconversion_choice

    async def recording(*args: Any, **kwargs: Any) -> Any:
        offered.append(True)
        return await choose(*args, **kwargs)

    # A new group on RED.
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        input="\n\n",
        tracker=NoGroupIdTracker(),
        prompt_downconversion_choice=recording,
    )

    assert run.result.exit_code == 0, run.result.output
    url = f"{run.tracker.url}/torrents.php?torrentid={FIRST_TORRENT_ID}"
    assert f"No group id came back for {url}: no conversions are offered for it." in run.result.output
    # Each conversion would have started a group of its own: none is offered, nor sent.
    assert offered == []
    assert len(_uploads(run)) == 1
    # Nor is group 0's page asked for.
    assert not [sent for sent in run.tracker.sent if sent.query.get("action") == "torrentgroup"]
    assert seedbox_runs == [True]
    assert len(run.queued) == 2


def test_a_machine_with_no_clipboard_still_reports_and_seeds_each_upload(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED")
    seedbox_runs = _seedbox_runs(monkeypatch)
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", True)

    def no_clipboard(_text: str) -> None:
        raise pyperclip.PyperclipException("Pyperclip could not find a copy/paste mechanism for your system.")

    monkeypatch.setattr(pyperclip, "copy", no_clipboard)

    # A new group on RED.
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\n\n")

    assert run.result.exit_code == 0, run.result.output
    output = run.result.output
    # The FLAC and its two transcodes, each reported and queued for the seedbox.
    assert len(_uploads(run)) == 3
    assert output.count("Successfully uploaded") == 3
    assert output.count("Could not copy to the clipboard: Pyperclip could not find") == 3
    assert seedbox_runs == [True]
    assert len(run.queued) == 6


def test_a_torrent_the_disk_cannot_take_the_url_into_still_seeds_as_uploaded(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED")
    seedbox_runs = _seedbox_runs(monkeypatch)
    write = Torrent.write
    failed: list[str] = []

    def full_disk(self: Torrent, filepath: str, validate: bool = True, overwrite: bool = False) -> None:
        if not self.comment:
            # The .torrent the upload is made from.
            return write(self, filepath, validate=validate, overwrite=overwrite)
        # With its URL, once uploaded: part of it, then the disk is full.
        failed.append(os.path.basename(filepath))
        Path(filepath).write_bytes(self.dump()[:100])
        raise WriteError(errno.ENOSPC, filepath)

    monkeypatch.setattr(Torrent, "write", full_disk)

    # A new group on RED.
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\n\n")

    assert run.result.exit_code == 0, run.result.output
    output = run.result.output
    assert len(failed) == 3
    assert len(_uploads(run)) == 3
    assert output.count("Successfully uploaded") == 3
    assert output.count("it seeds without it.") == 3
    # Each .torrent is the one uploaded, whole, with nothing left beside it.
    dot_torrents = sorted(torrents.iterdir())
    assert [path.suffix for path in dot_torrents] == [".torrent"] * 3
    sent = sorted(Torrent.read_stream(dict(upload)["file_input"][1]).infohash for upload in _sent_files(run))
    assert sorted(Torrent.read(path).infohash for path in dot_torrents) == sent
    assert seedbox_runs == [True]
    assert len(run.queued) == 6


def _sent_files(run) -> list[list[tuple[str, Any]]]:
    return [sent.fields for sent in run.tracker.sent if sent.query.get("action") == "upload"]


def _uploaded_torrent(tmp_path: Path) -> tuple[Torrent, Path]:
    """A .torrent as generate_torrent leaves it, and its torrent with the URL added, ready to rewrite."""
    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"fLaC" + bytes(40000))
    torrent = Torrent(path=folder, trackers=["https://tracker.invalid/announce"], private=True)
    torrent.generate()
    torrent_path = tmp_path / "torrents" / "Album.torrent"
    torrent_path.parent.mkdir()
    torrent.write(torrent_path)
    torrent.comment = "https://tracker.invalid/torrents.php?torrentid=1"
    return torrent, torrent_path


@pytest.mark.parametrize("fails", [False, True], ids=["written", "disk full"])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_the_torrent_rewrite_touches_no_file_but_its_own(monkeypatch, tmp_path, kind, fails) -> None:
    torrent, torrent_path = _uploaded_torrent(tmp_path)
    original = torrent_path.read_bytes()
    # Someone else's file at the name a fixed temporary file would have, or a link from there to one.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"not salmon's")
    beside = Path(f"{torrent_path}.part")
    if kind == "file":
        beside.write_bytes(b"not salmon's")
    else:
        beside.symlink_to(elsewhere)
    if fails:

        def full_disk(self: Torrent, filepath: str, validate: bool = True, overwrite: bool = False) -> None:
            Path(filepath).write_bytes(self.dump()[:100])
            raise WriteError(errno.ENOSPC, filepath)

        monkeypatch.setattr(Torrent, "write", full_disk)

    salmon.uploader._rewrite_torrent(torrent, str(torrent_path))

    assert beside.read_bytes() == b"not salmon's"
    assert beside.is_symlink() == (kind == "symlink")
    assert elsewhere.read_bytes() == b"not salmon's"
    assert sorted(path.name for path in torrent_path.parent.iterdir()) == ["Album.torrent", "Album.torrent.part"]
    if fails:
        assert torrent_path.read_bytes() == original
    else:
        assert Torrent.read(torrent_path).comment == torrent.comment


def test_the_rewritten_torrent_keeps_the_mode_of_the_one_it_replaces(tmp_path) -> None:
    torrent, torrent_path = _uploaded_torrent(tmp_path)
    # Readable by a torrent client running as another user of the same group.
    torrent_path.chmod(0o640)

    salmon.uploader._rewrite_torrent(torrent, str(torrent_path))

    assert Torrent.read(torrent_path).comment == torrent.comment
    assert torrent_path.stat().st_mode & 0o777 == 0o640


# remove_local_after_copy runs only after a run that ended by itself with every upload done (#754)


def _removals(monkeypatch) -> list[str | None]:
    """Record each call of remove_local_copies with its keep_because, instead of removing anything."""
    calls: list[str | None] = []
    monkeypatch.setattr(
        UploadManager, "remove_local_copies", lambda _self, keep_because=None: calls.append(keep_because)
    )
    return calls


def test_a_clean_run_asks_for_the_local_copies_to_be_removed(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED")
    removals = _removals(monkeypatch)
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\n\n")
    assert run.result.exit_code == 0, run.result.output
    assert removals == [None]


@pytest.mark.parametrize("error", [RequestError("RED is down"), UnknownOutcomeError("RED hung up")])
def test_a_failed_tracker_upload_keeps_the_local_copies(monkeypatch, tmp_path, dirs, error) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    removals = _removals(monkeypatch)
    _checks(monkeypatch)
    _description_edits(monkeypatch)
    upload_and_report = salmon.uploader.upload_and_report

    async def ops_fails(gazelle_site, *args, **kwargs) -> Any:
        if gazelle_site.site_code == "OPS":
            raise error
        return await upload_and_report(gazelle_site, *args, **kwargs)

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        args=("-a",),
        input="\nOPS\n\n\n",
        upload_and_report=ops_fails,
    )
    assert run.result.exit_code == 0, run.result.output
    assert removals == ["the upload to OPS failed or was skipped"]


def test_an_abort_after_an_upload_keeps_the_local_copies_and_still_seeds(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    removals = _removals(monkeypatch)
    seedbox_runs = _seedbox_runs(monkeypatch)
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\nOPS\na\n")
    assert run.result.exit_code == 0, run.result.output
    assert "Aborting: nothing more is uploaded." in run.result.output
    assert seedbox_runs == [True]
    assert removals == ["the run stopped before it finished"]


def test_an_unexpected_error_keeps_the_local_copies_and_propagates(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED")
    removals = _removals(monkeypatch)
    seedbox_runs = _seedbox_runs(monkeypatch)

    async def breaks(gazelle_site, *args, **kwargs) -> Any:
        raise RuntimeError("disk on fire")

    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\n\n", upload_and_report=breaks)
    assert isinstance(run.result.exception, RuntimeError)
    assert seedbox_runs == [True]
    assert removals == ["the run stopped before it finished"]


def test_a_skipped_tracker_keeps_the_local_copies(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    removals = _removals(monkeypatch)
    _checks(monkeypatch)
    _description_edits(monkeypatch)
    resolve_cover_url = salmon.uploader.resolve_cover_url

    async def ops_has_no_cover(gazelle_site, *args, **kwargs) -> Any:
        if gazelle_site.site_code == "OPS":
            return False, None
        return await resolve_cover_url(gazelle_site, *args, **kwargs)

    monkeypatch.setattr(salmon.uploader, "resolve_cover_url", ops_has_no_cover)
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, args=("-a",), input="\nOPS\n\n\n")
    assert run.result.exit_code == 0, run.result.output
    assert "Skipping upload to OPS." in run.result.output
    assert removals == ["the upload to OPS failed or was skipped"]


def test_a_tracker_refused_before_its_upload_keeps_the_local_copies(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    removals = _removals(monkeypatch)
    _checks(monkeypatch)
    _description_edits(monkeypatch)
    refusal = salmon.uploader._do_not_upload_refusal

    def ops_refused(tracker: str, release: dict[str, Any], said: str | None = None) -> str | None:
        return "refused" if tracker == "OPS" else refusal(tracker, release, said)

    monkeypatch.setattr(salmon.uploader, "_do_not_upload_refusal", ops_refused)
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, args=("-a",), input="\nOPS\n\n\n")
    assert run.result.exit_code == 0, run.result.output
    assert removals == ["the upload to OPS failed or was skipped"]


def test_a_first_tracker_refused_before_the_loop_keeps_the_local_copies(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED", "OPS")
    removals = _removals(monkeypatch)
    _checks(monkeypatch)
    _description_edits(monkeypatch)
    refusal = salmon.uploader._do_not_upload_refusal

    def red_refused(tracker: str, release: dict[str, Any], said: str | None = None) -> str | None:
        return "refused" if tracker == "RED" else refusal(tracker, release, said)

    monkeypatch.setattr(salmon.uploader, "_do_not_upload_refusal", red_refused)
    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, args=("-a",), input="OPS\n\n\n")
    assert run.result.exit_code == 0, run.result.output
    assert len(_uploads(run)) > 0, run.result.output
    assert removals == ["the upload to RED failed or was skipped"]
