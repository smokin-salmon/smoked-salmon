"""salmon up and a DSD album (#919): RED takes it as DSD, OPS and DIC refuse it before any request.

The runs go against the local fake tracker of helpers.uploader_dry_run, never a real one.
"""

import copy
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers.dsd import DSD64, write_dsf
from helpers.uploader_dry_run import (
    METADATA,
    RLS_DATA,
    _printed_uploads,
    _returning,
    _returning_async,
    _run_up,
)
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see images)
)
from torf import Torrent

from salmon import cfg
from salmon.tagger.folderstructure import check_folder_structure
from salmon.trackers.dic import DICApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader.upload_checks import _dsd_refusal

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("images")

NAMES = ("01 - one.dsf", "02 - two.dsf")
OPS_REFUSED = "Not uploading to OPS: OPS does not take DSD."
DIC_REFUSED = "Not uploading to DIC: salmon does not know whether DIC takes DSD, so it sends it none."


@pytest.fixture
def images(image_uploads) -> list[tuple[str, str]]:  # noqa: F811
    """The (file name, URL) of each image the fake image host took."""
    return image_uploads


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


def _dsd_album(folder: Path) -> Path:
    folder.mkdir(parents=True)
    for number, name in enumerate(NAMES, 1):
        write_dsf(folder / name, tags={"TIT2": name, "TPE1": "Artist", "TALB": "Album", "TRCK": str(number)})
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


def _dsd(**fakes):
    """The fakes of a run whose files are DSD64: what the metadata step reads from them, and its review gives."""
    audio_info = {
        name: {"sample rate": DSD64, "precision": 1, "duration": 60, "bit rate": DSD64 * 2, "channels": 2}
        for name in NAMES
    }
    rls_data = {**copy.deepcopy(RLS_DATA), "format": "DSD", "encoding": "DSD64"}
    metadata = {**copy.deepcopy(METADATA), "format": "DSD", "encoding": "DSD64"}
    track_data = {
        name: {**info, "t": SimpleNamespace(tracknumber=str(number), discnumber=None, artist=["Artist"], title=name)}
        for number, (name, info) in enumerate(audio_info.items(), 1)
    }
    return {
        "gather_audio_info": _returning(audio_info),
        "construct_rls_data": _returning_async(rls_data),
        "get_metadata": _returning_async((metadata, None)),
        "review_metadata_with_ai": _returning_async(metadata),
        "concat_track_data": _returning(track_data),
        **fakes,
    }


def _uploads(run) -> list[dict]:
    """The form of each torrent uploaded, with the tracker it is for (its source flag) as "tracker"."""
    forms = []
    for sent in run.tracker.sent:
        if sent.query.get("action") == "upload":
            fields = dict(sent.fields)
            torrent = Torrent.read_stream(fields["file_input"][1])
            forms.append({**fields, "tracker": str(torrent.source), "files": sorted(f.name for f in torrent.files)})
    return forms


def _requests(run) -> list[tuple[str, str, str | None]]:
    return [(sent.method, sent.path, sent.query.get("action")) for sent in run.tracker.sent]


def test_a_dsd_album_goes_to_red_as_dsd_with_its_rate_and_no_conversion(monkeypatch, dirs) -> None:
    downloads, torrents = dirs

    run = _run_up(
        monkeypatch, _dsd_album(downloads.parent / "seeding" / "Album"), torrents, multi_tracker_upload=False, **_dsd()
    )

    assert run.result.exit_code == 0, run.result.output
    (form,) = _uploads(run)
    assert (form["tracker"], form["format"], form["bitrate"], form["media"]) == ("RED", "DSD", "DSD64", "WEB")
    assert form["files"] == sorted(NAMES + ("cover.jpg",))
    assert "DSD64 [color=#2E86C1]2.8224[/color] MHz" in form["release_desc"]
    assert "1 bit" not in form["release_desc"]


def test_a_dry_run_of_a_dsd_album_prints_its_red_form(monkeypatch, dirs) -> None:
    downloads, torrents = dirs

    run = _run_up(
        monkeypatch,
        _dsd_album(downloads.parent / "seeding" / "Album"),
        torrents,
        args=("--dry-run",),
        multi_tracker_upload=False,
        **_dsd(),
    )

    assert run.result.exit_code == 0, run.result.output
    assert run.tracker.not_gets() == []
    ((line, fields, _torrent),) = _printed_uploads(run.result.output)
    assert line.startswith("Dry run: not uploading to RED")
    assert (dict(fields)["format"], dict(fields)["bitrate"]) == ("DSD", "DSD64")


@pytest.mark.parametrize(
    ("tracker", "refused"), [("OPS", OPS_REFUSED), ("DIC", DIC_REFUSED)], ids=["OPS takes none", "DIC not known"]
)
def test_a_dsd_album_is_refused_before_any_request(monkeypatch, dirs, images, tracker: str, refused: str) -> None:
    downloads, torrents = dirs
    reached: list[str] = []

    async def mqa_test(*_args, **_kwargs) -> None:
        reached.append("checks")

    # One tracker, and -yyy: nothing skips the refusal.
    run = _run_up(
        monkeypatch,
        _dsd_album(downloads.parent / "seeding" / "Album"),
        torrents,
        trackers=(tracker,),
        multi_tracker_upload=False,
        classes={"RED": RedApi, "OPS": OpsApi, "DIC": DICApi},
        **_dsd(mqa_test=mqa_test),
    )

    assert run.result.exit_code == 1, run.result.output
    assert run.result.output.count(refused) == 1
    assert "Traceback" not in run.result.output
    assert run.tracker.not_gets() == []
    assert "browse" not in {sent.query.get("action") for sent in run.tracker.sent}
    assert reached == []
    assert images == []


def test_a_dsd_album_for_red_then_ops_goes_to_red_alone(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    alone = _run_up(
        monkeypatch, _dsd_album(tmp_path / "alone" / "Album"), torrents, multi_tracker_upload=False, **_dsd()
    )
    assert alone.result.exit_code == 0, alone.result.output
    # The next run's renamed folder takes the same name; salmon never replaces this one, hardlinked to its album.
    for made in Path(cfg.directory.download_directory).iterdir():
        shutil.rmtree(made)

    run = _run_up(
        monkeypatch,
        _dsd_album(downloads.parent / "seeding" / "Album"),
        torrents,
        trackers=("RED", "OPS"),
        **_dsd(),
    )

    assert run.result.exit_code == 0, run.result.output
    assert f"Next tracker: OPS\n\n{OPS_REFUSED}" in run.result.output
    assert [form["tracker"] for form in _uploads(run)] == ["RED"]
    # OPS got nothing, not even its dupe check.
    assert _requests(run) == _requests(alone)


@pytest.mark.parametrize(("tracker", "refused"), [("RED", False), ("OPS", True), ("DIC", True)])
def test_only_a_dsd_release_is_refused_and_only_where_dsd_is_not_taken(tracker: str, refused: bool) -> None:
    assert _dsd_refusal(tracker, {"format": "DSD"}) is refused
    assert _dsd_refusal(tracker, {"format": "FLAC"}) is False


def test_the_folder_check_keeps_the_dsd_files_in_the_torrent(monkeypatch, dirs) -> None:
    # Before DSD was known, the folder check deleted .dsf files under -yyy, as files a torrent can't hold.
    downloads, torrents = dirs

    run = _run_up(
        monkeypatch,
        _dsd_album(downloads.parent / "seeding" / "Album"),
        torrents,
        multi_tracker_upload=False,
        **_dsd(check_folder_structure=check_folder_structure),
    )

    assert run.result.exit_code == 0, run.result.output
    assert "File extensions have been validated." in run.result.output
    (form,) = _uploads(run)
    assert form["files"] == sorted(NAMES + ("cover.jpg",))
