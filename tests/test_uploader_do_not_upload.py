"""salmon up and the trackers' Do-Not-Upload lists (#533): a listed release never goes to that tracker.

The runs go against the local fake tracker of test_uploader_dry_run, never a real one.
"""

from pathlib import Path

import pytest
from test_checks_do_not_upload import write_lists  # pyright: ignore[reportMissingImports]
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    _album,
    _returning_async,
    _run_up,
    image_uploads,  # noqa: F401 (a fixture: see images)
)
from torf import Torrent

from salmon import cfg

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("images")

ARTIST = "[[entry]]\nartist = 'Artist'\nnote = 'Fakes only.'\n"


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


def _uploaded_to(run) -> list[str]:
    """The tracker each uploaded torrent is for, by its source flag."""
    return [
        str(Torrent.read_stream(dict(sent.fields)["file_input"][1]).source)
        for sent in run.tracker.sent
        if sent.query.get("action") == "upload"
    ]


def test_a_release_on_reds_list_goes_to_ops_only(monkeypatch, tmp_path, dirs, images) -> None:
    downloads, torrents = dirs
    write_lists(monkeypatch, tmp_path / "lists", RED=ARTIST)

    run = _run_up(monkeypatch, _album(downloads.parent / "seeding" / "Album"), torrents)

    assert run.result.exit_code == 0, run.result.output
    assert (
        "Not uploading to RED: Artist (the whole discography) is on RED's Do-Not-Upload list: Fakes only. If yours "
        "is a legitimate copy, RED wants a message to its staff, with proof, before it is uploaded."
    ) in run.result.output
    # The FLAC and its two transcodes, to OPS only. The cover and spectrals once, for OPS: not the spectrals first,
    # as for a first tracker that is not listed.
    assert _uploaded_to(run) == ["OPS"] * 3
    assert [name for name, _url in images] == ["cover.jpg", "01 Full.png", "01 Zoom.png"]


def _requests(run) -> list[tuple[str, str, str | None]]:
    return [(sent.method, sent.path, sent.query.get("action")) for sent in run.tracker.sent]


def test_a_release_on_opss_list_goes_to_red_only(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    red_only = _run_up(monkeypatch, _album(tmp_path / "alone" / "Album"), torrents, multi_tracker_upload=False)
    assert red_only.result.exit_code == 0, red_only.result.output
    write_lists(monkeypatch, tmp_path / "lists", OPS=ARTIST)

    # OPS named after RED: it is not asked for.
    run = _run_up(monkeypatch, _album(downloads.parent / "seeding" / "Album"), torrents, args=("-t", "OPS"))

    assert run.result.exit_code == 0, run.result.output
    assert "Next tracker: OPS\n\nNot uploading to OPS: Artist (the whole discography) is on OPS's" in run.result.output
    assert _uploaded_to(run) == ["RED"] * 3
    # OPS got nothing, not even its dupe check: the run sent what a run to RED alone sends.
    assert _requests(run) == _requests(red_only)


def test_a_listed_last_tracker_ends_the_run_cleanly(monkeypatch, tmp_path, dirs, images) -> None:
    downloads, torrents = dirs
    write_lists(monkeypatch, tmp_path / "lists", RED=ARTIST)

    # One tracker, and -yyy: nothing skips the list.
    run = _run_up(monkeypatch, _album(downloads.parent / "seeding" / "Album"), torrents, multi_tracker_upload=False)

    assert run.result.exit_code == 0, run.result.output
    assert "Not uploading to RED: Artist (the whole discography)" in run.result.output
    assert "Traceback" not in run.result.output
    # Nothing was sent or uploaded for RED, and it was not even searched.
    assert run.tracker.not_gets() == []
    assert "browse" not in {sent.query.get("action") for sent in run.tracker.sent}
    assert images == []
    assert run.queued == []


def _announcing(name: str):
    """A stand-in for a check that only says it ran, and finds nothing."""

    async def fake(*_args, **_kwargs):
        print(f"{name} ran")
        return False, None

    return fake


def test_a_release_listed_by_its_tags_is_refused_before_the_group_search_and_its_prompt(
    monkeypatch, tmp_path, dirs
) -> None:
    downloads, torrents = dirs
    write_lists(monkeypatch, tmp_path / "lists", RED=ARTIST)
    groups_resolved: list[object] = []

    async def resolve_existing_group(*args, **_kwargs):
        groups_resolved.append(args)
        return None

    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        multi_tracker_upload=False,
        resolve_existing_group=resolve_existing_group,
        check_spectrals=_announcing("check_spectrals"),
    )

    assert run.result.exit_code == 0, run.result.output
    # Said once, before the checks that follow the tags (the spectrals here), and not again after the review.
    assert run.result.output.count("Not uploading to RED: Artist (the whole discography)") == 1
    assert run.result.output.index("Not uploading to RED") < run.result.output.index("check_spectrals ran")
    assert groups_resolved == []
    assert "browse" not in {sent.query.get("action") for sent in run.tracker.sent}


def test_a_release_the_review_takes_off_the_list_is_searched_and_uploaded(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    write_lists(monkeypatch, tmp_path / "lists", RED="[[entry]]\nartist = 'Tagged Wrong'\nnote = 'Fakes only.'\n")
    # The tags name a listed artist; the review corrects it to "Artist".
    tags = {"format": "FLAC", "encoding": "Lossless", "artists": [("Tagged Wrong", "main")], "title": "Album"}

    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        multi_tracker_upload=False,
        construct_rls_data=lambda *_args, **_kwargs: {**tags, "catno": "CAT1"},
    )

    assert run.result.exit_code == 0, run.result.output
    assert "Not uploading to RED: Tagged Wrong (the whole discography)" in run.result.output
    assert "As reviewed, the release is not on RED's Do-Not-Upload list." in run.result.output
    # The group search skipped for the tags runs on the reviewed names, then the upload.
    assert "browse" in {sent.query.get("action") for sent in run.tracker.sent}
    assert _uploaded_to(run) == ["RED"] * 3


def test_a_list_salmon_cannot_read_stops_that_tracker_only(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    write_lists(monkeypatch, tmp_path / "lists", RED="[[entry]]\nartist = 'Someone'\n")

    run = _run_up(monkeypatch, _album(downloads.parent / "seeding" / "Album"), torrents)

    assert run.result.exit_code == 0, run.result.output
    assert "Not uploading to RED: salmon cannot read its copy of RED's Do-Not-Upload list" in run.result.output
    assert _uploaded_to(run) == ["OPS"] * 3


def test_with_skip_flac_upload_a_listed_tracker_ends_the_run(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    write_lists(monkeypatch, tmp_path / "lists", RED=ARTIST)

    # The transcodes go into the RED group of the FLAC: no other tracker is offered instead.
    run = _run_up(
        monkeypatch,
        _album(downloads.parent / "seeding" / "Album"),
        torrents,
        args=("-g", "55", "--skip-flac-upload"),
        input="y\n",
        check_spectrals=_returning_async((False, None)),
    )

    assert run.result.exit_code == 0, run.result.output
    assert "Not uploading to RED: Artist (the whole discography)" in run.result.output
    assert "another tracker" not in run.result.output
    assert run.tracker.not_gets() == []
    assert run.queued == []
