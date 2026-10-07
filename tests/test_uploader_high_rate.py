"""salmon up and 16bit files above 48 kHz (#618): OPS refuses them, RED warns, other files are untouched.

The runs go against the local fake tracker of test_uploader_dry_run, never a real one.
"""

from pathlib import Path

import pytest
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    _album,
    _returning,
    _returning_async,
    _run_up,
    image_uploads,  # noqa: F401 (a fixture: see images)
)
from torf import Torrent

from salmon import cfg

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("images")

FILES = "01 - one.flac (96 kHz); 02 - two.flac (96 kHz)"
REFUSED = f"Not uploading to OPS: 2 16bit file(s) above 48 kHz: {FILES}. OPS refuses them."
TRUMPABLE = f"2 16bit file(s) above 48 kHz: {FILES}. RED can trump them."


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


def _files(rate: int, bits: int = 16) -> dict[str, dict]:
    """The audio info of the album's two files."""
    return {
        name: {"sample rate": rate, "precision": bits, "duration": 60, "bit rate": 1_000_000, "channels": 2}
        for name in ("01 - one.flac", "02 - two.flac")
    }


def _uploaded_to(run) -> list[str]:
    """The tracker each uploaded torrent is for, by its source flag."""
    return [
        str(Torrent.read_stream(dict(sent.fields)["file_input"][1]).source)
        for sent in run.tracker.sent
        if sent.query.get("action") == "upload"
    ]


def _requests(run) -> list[tuple[str, str, str | None]]:
    return [(sent.method, sent.path, sent.query.get("action")) for sent in run.tracker.sent]


def _red_alone(monkeypatch, tmp_path, torrents):
    """The run to RED alone that a run with OPS refused must send the same requests as."""
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "alone" / "Album"),
        torrents,
        multi_tracker_upload=False,
        gather_audio_info=_returning(_files(96000)),
    )
    assert run.result.exit_code == 0, run.result.output
    return run


def test_16bit_96khz_is_refused_for_ops_before_any_request_and_its_group_search(monkeypatch, dirs, images) -> None:
    downloads, torrents = dirs
    groups_resolved: list[object] = []

    async def resolve_existing_group(*args, **_kwargs):
        groups_resolved.append(args)

    # One tracker, and -yyy: nothing skips the refusal.
    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        trackers=("OPS",),
        multi_tracker_upload=False,
        gather_audio_info=_returning(_files(96000)),
        resolve_existing_group=resolve_existing_group,
    )

    assert run.result.exit_code == 0, run.result.output
    assert run.result.output.count(REFUSED) == 1
    assert "Traceback" not in run.result.output
    assert groups_resolved == []
    assert run.tracker.not_gets() == []
    assert "browse" not in {sent.query.get("action") for sent in run.tracker.sent}
    assert images == []
    assert run.queued == []


def test_16bit_96khz_is_warned_about_for_red_and_uploaded(monkeypatch, dirs) -> None:
    downloads, torrents = dirs

    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        multi_tracker_upload=False,
        gather_audio_info=_returning(_files(96000)),
    )

    assert run.result.exit_code == 0, run.result.output
    assert TRUMPABLE in run.result.output
    assert "Not uploading" not in run.result.output
    assert _uploaded_to(run) == ["RED"] * 3


def test_16bit_96khz_for_ops_then_red_refuses_ops_and_uploads_to_red(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    red_only = _red_alone(monkeypatch, tmp_path, torrents)

    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        trackers=("OPS", "RED"),
        gather_audio_info=_returning(_files(96000)),
    )

    assert run.result.exit_code == 0, run.result.output
    assert REFUSED in run.result.output
    assert TRUMPABLE in run.result.output
    # The FLAC and its two transcodes, to RED only: OPS got nothing, not even its dupe check.
    assert _uploaded_to(run) == ["RED"] * 3
    assert _requests(run) == _requests(red_only)


def test_16bit_96khz_for_red_then_ops_refuses_ops_before_its_dupe_check(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    red_only = _red_alone(monkeypatch, tmp_path, torrents)

    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        trackers=("RED", "OPS"),
        gather_audio_info=_returning(_files(96000)),
    )

    assert run.result.exit_code == 0, run.result.output
    assert f"Next tracker: OPS\n\n{REFUSED}" in run.result.output
    assert _uploaded_to(run) == ["RED"] * 3
    assert _requests(run) == _requests(red_only)


@pytest.mark.parametrize(("rate", "bits"), [(48000, 16), (96000, 24), (44100, 16)], ids=["16/48", "24/96", "16/44.1"])
def test_other_files_go_to_ops_unremarked(monkeypatch, dirs, rate: int, bits: int) -> None:
    downloads, torrents = dirs

    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        trackers=("OPS",),
        multi_tracker_upload=False,
        gather_audio_info=_returning(_files(rate, bits)),
    )

    assert run.result.exit_code == 0, run.result.output
    assert "16bit file(s) above 48 kHz" not in run.result.output
    assert _uploaded_to(run) == ["OPS"] * 3


def test_16bit_96khz_with_skip_flac_upload_is_not_refused_for_ops_as_only_transcodes_go_up(
    monkeypatch, tmp_path, dirs
) -> None:
    _downloads, torrents = dirs

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        torrents,
        args=("--dry-run", "-g", "55", "--skip-flac-upload"),
        input="y\n",
        trackers=("OPS",),
        gather_audio_info=_returning(_files(96000)),
        check_spectrals=_returning_async((False, None)),
    )

    assert run.result.exit_code == 0, run.result.output
    assert "16bit file(s) above 48 kHz" not in run.result.output
    assert "Not uploading to OPS" not in run.result.output
    assert "320" in run.result.output
