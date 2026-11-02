"""salmon up --cover-url / --spectral-url: images uploaded already are used instead of uploaded again (#795).

The runs here go against the local fake tracker of helpers.uploader_dry_run and the fake image host of
test_uploader_dry_run, never a real one. The URLs given are on real hosts' domains, but nothing ever fetches them.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from helpers.uploader_dry_run import (
    _album,
    _run_up,
    _torrent_lines,
)
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401
)

import salmon.images
import salmon.trackers
import salmon.uploader
from salmon import cfg
from salmon.images import host_of_url
from salmon.uploader.given_images import parse_spectral_urls, url_refusal
from salmon.uploader.spectrals import SpectralUploads, make_spectral_bbcode

pytestmark = pytest.mark.usefixtures("image_uploads")

COVER = "https://files.catbox.moe/cover1.jpg"
FULL_1, ZOOM_1, FULL_2 = (f"https://files.catbox.moe/spec{n}.png" for n in (1, 2, 3))
RED_IMAGE = "https://redacted.sh/i/x00011.png"


@pytest.fixture(autouse=True)
def red_and_ops(monkeypatch) -> None:
    """RED and OPS are the configured trackers: a run with no -t offers OPS after RED."""
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path, Path]:
    """A library, a download_directory and a dot_torrents_dir, configured."""
    library, downloads, torrents = tmp_path / "library", tmp_path / "downloads", tmp_path / "torrents"
    for folder in (library, downloads, torrents):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    return library, downloads, torrents


@pytest.fixture
def uploaded(request) -> list[tuple[str, str]]:
    """The (file name, URL) of each image the fake host of test_uploader_dry_run took."""
    return request.getfixturevalue("image_uploads")


def _uploads(run) -> list[dict[str, Any]]:
    """The form of each torrent the run uploaded, in order."""
    return [dict(sent.fields) for sent in run.tracker.sent if sent.query.get("action") == "upload"]


def _reports(run) -> list[str]:
    """The lossy master report sent with each torrent, in order."""
    return [dict(sent.fields)["extra"] for sent in run.tracker.sent if sent.path == "/reportsv2.php"]


async def _no_spectrals(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("spectrals were made")


def _refused(run, message: str) -> None:
    """The run stopped at the start with message, before any request to the tracker."""
    assert run.result.exit_code == 2, run.result.output
    assert message in run.result.output
    assert run.tracker.sent == []


# The cover


def test_a_cover_url_stands_for_the_cover_on_every_tracker(monkeypatch, tmp_path, dirs, uploaded) -> None:
    _library, _downloads, torrents = dirs

    run = _run_up(monkeypatch, _album(tmp_path / "seeding" / "Album"), torrents, args=("--cover-url", COVER))

    assert run.result.exit_code == 0, run.result.output
    # The spectrals still go up; the cover does not.
    assert [name for name, _url in uploaded] == ["01 Full.png", "01 Zoom.png"]
    uploads = _uploads(run)
    # The FLAC to RED and to OPS, a new group each, then their transcodes into it, which send no image.
    assert len(uploads) == 6
    assert [upload.get("image") for upload in uploads] == [COVER, None, None] * 2


@pytest.mark.parametrize("tracker", ["RED", "OPS"])
def test_a_cover_on_reds_image_host_goes_to_red_and_ops(monkeypatch, tmp_path, dirs, tracker: str) -> None:
    _library, _downloads, torrents = dirs

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        torrents,
        args=("--cover-url", RED_IMAGE),
        input="\n",
        trackers=(tracker,),
    )

    assert run.result.exit_code == 0, run.result.output
    uploads = _uploads(run)
    assert uploads, run.result.output
    assert uploads[0]["image"] == RED_IMAGE


def test_a_cover_on_reds_image_host_is_refused_for_dic(monkeypatch, tmp_path, dirs) -> None:
    _library, _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        torrents,
        args=("--cover-url", RED_IMAGE),
        trackers=("RED", "DIC"),
    )

    _refused(run, "--cover-url: https://redacted.sh/i/x00011.png is on red, which can't be used for DIC's images")


def test_a_run_that_may_offer_dic_refuses_a_cover_on_reds_image_host(monkeypatch, tmp_path, dirs) -> None:
    _library, _downloads, torrents = dirs
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])

    # No -t, and multi_tracker_upload: DIC is offered after RED.
    run = _run_up(monkeypatch, _album(tmp_path / "seeding" / "Album"), torrents, args=("--cover-url", RED_IMAGE))

    _refused(run, "can't be used for DIC's images")


def test_a_cover_on_a_host_salmon_does_not_know_is_refused(monkeypatch, tmp_path, dirs) -> None:
    _library, _downloads, torrents = dirs

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        torrents,
        args=("--cover-url", "https://images.example.com/cover.jpg"),
    )

    _refused(run, "--cover-url: images.example.com is not one of salmon's image hosts (")


# The spectrals


def test_spectral_urls_stand_for_the_spectrals(monkeypatch, tmp_path, dirs, uploaded) -> None:
    _library, _downloads, torrents = dirs
    album = _album(tmp_path / "seeding" / "Album")

    run = _run_up(
        monkeypatch,
        album,
        torrents,
        # Track 2 first: the description keeps the order given.
        args=("--spectral-url", f"2={FULL_2}", "--spectral-url", f"1={FULL_1},{ZOOM_1}", "--lossy"),
        check_spectrals=_no_spectrals,
    )

    assert run.result.exit_code == 0, run.result.output
    assert "Using the spectrals given by URL for tracks 2, 1." in run.result.output
    assert "Uploading spectrals" not in run.result.output
    assert "Spectrals given by URL (--spectral-url): none are made, shown or uploaded." in run.result.output
    # Only the cover went up, and no spectrals folder was made.
    assert [name for name, _url in uploaded] == ["cover.jpg"]
    assert not list(tmp_path.rglob("Spectrals"))
    uploads, reports = _uploads(run), _reports(run)
    assert len(uploads) == len(reports) == 6
    description = uploads[0]["release_desc"]
    # Each track under its file's name, with its Zoom image hidden when it has one.
    assert (
        f"[b]02 - two.flac Full[/b]\n[img={FULL_2}]\n\n"
        f"[b]01 - one.flac Full[/b]\n[img={FULL_1}]\n[hide=Zoomed][img={ZOOM_1}][/hide]\n\n"
    ) in description
    for text in (uploads[3]["release_desc"], *reports):
        assert all(url in text for url in (FULL_1, ZOOM_1, FULL_2))


def test_the_lossy_master_question_is_asked_with_spectral_urls(monkeypatch, tmp_path, dirs) -> None:
    _library, _downloads, torrents = dirs
    asked: list[bool] = []

    async def prompt_lossy_master(*_args: Any, **_kwargs: Any) -> bool:
        asked.append(True)
        return True

    monkeypatch.setattr(salmon.uploader, "prompt_lossy_master", prompt_lossy_master)

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        torrents,
        args=("--spectral-url", f"1={FULL_1},{ZOOM_1}"),
        check_spectrals=_no_spectrals,
    )

    assert run.result.exit_code == 0, run.result.output
    assert asked == [True]
    # Asked once for the run: the lossy master report goes with each torrent, with the spectrals given.
    assert len(_reports(run)) == 6
    assert all(FULL_1 in report and ZOOM_1 in report for report in _reports(run))


def test_not_lossy_answers_the_lossy_master_question(monkeypatch, tmp_path, dirs) -> None:
    _library, _downloads, torrents = dirs

    async def prompt_lossy_master(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("asked whether the release is lossy mastered")

    monkeypatch.setattr(salmon.uploader, "prompt_lossy_master", prompt_lossy_master)

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        torrents,
        args=("--spectral-url", f"1={FULL_1},{ZOOM_1}", "--not-lossy"),
        check_spectrals=_no_spectrals,
    )

    assert run.result.exit_code == 0, run.result.output
    assert _reports(run) == []
    assert FULL_1 in _uploads(run)[0]["release_desc"]


def test_reopening_spectrals_given_by_url_asks_again(monkeypatch, capsys) -> None:
    answers = iter([None, False])

    async def prompt_lossy_master(*_args: Any, **_kwargs: Any) -> bool | None:
        return next(answers)

    monkeypatch.setattr(salmon.uploader, "prompt_lossy_master", prompt_lossy_master)

    assert anyio.run(salmon.uploader._lossy_master_without_spectrals, None, "FLAC") is False
    assert "There are no spectrals to reopen: they were given by URL." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--spectral-url", FULL_1), f"'{FULL_1}' is not TRACK=URL[,URL]"),
        (("--spectral-url", f"one={FULL_1}"), "'one' in"),
        (("--spectral-url", f"0={FULL_1}"), "'0' in"),
        (("--spectral-url", f"1={FULL_1}", "--spectral-url", f"1={ZOOM_1}"), "track 1 is given twice"),
        (("--spectral-url", f"1={FULL_1},{ZOOM_1},{FULL_2}"), "has 3 URLs: a track takes at most two"),
        (("--spectral-url", f"1={FULL_1},"), "has an empty URL"),
        # A "]" would close the description's [img=] tag and add an image on a host the check never saw.
        (("--spectral-url", f"1={FULL_1}][img={RED_IMAGE}"), "has characters an image URL in a description cannot"),
        (("--cover-url", f"{COVER} [img={RED_IMAGE}]"), "--cover-url: "),
        (("--spectral-url", f"3={FULL_1}"), "--spectral-url: track 3 is not one of the album's"),
        (("--spectral-url", "1=ftp://files.catbox.moe/a.png"), "is not an http:// or https:// URL"),
        (("--spectral-url", f"1={FULL_1}?h=signature"), "give the image's own URL, with no query"),
        (("--spectral-url", "1=https://images.example.com/a.png"), "images.example.com is not one of salmon's"),
        (("--spectral-url", f"1={FULL_1}", "--spectrals", "1"), "--spectral-url cannot be used with --spectrals:"),
        (
            ("--spectral-url", f"1={FULL_1}", "--spectrals-after"),
            "--spectral-url cannot be used with --spectrals-after",
        ),
    ],
)
def test_a_spectral_url_that_cannot_be_used_stops_the_run_at_the_start(
    monkeypatch, tmp_path, dirs, uploaded, args: tuple[str, ...], message: str
) -> None:
    _library, _downloads, torrents = dirs

    run = _run_up(monkeypatch, _album(tmp_path / "seeding" / "Album"), torrents, args=args)

    _refused(run, message)
    assert uploaded == []


@pytest.mark.parametrize("tracker", ["RED", "OPS"])
@pytest.mark.parametrize(
    ("url", "reason"),
    [(RED_IMAGE, "RED's rules forbid spectrals on its image host"), ("https://thesungod.xyz/i/a.png", "Ra's owner")],
)
def test_a_spectral_on_a_host_that_may_not_take_spectrals_is_refused(
    monkeypatch, tmp_path, dirs, tracker: str, url: str, reason: str
) -> None:
    _library, _downloads, torrents = dirs

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        torrents,
        args=("--spectral-url", f"1={url}"),
        trackers=(tracker,),
    )

    _refused(run, f"can't be used for {tracker}'s spectrals: {reason}")


# The pieces


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://files.catbox.moe/a.png", "catbox"),
        ("https://i.ibb.co/abc/a.png", "imgbb"),
        ("https://images2.imgbox.com/aa/bb/a_o.png", "imgbox"),
        ("https://imgoe.download/images/a.png", "oeimg"),
        ("https://ptscreens.com/images/a.png", "ptscreens"),
        ("https://thesungod.xyz/i/a.png", "ra"),
        ("https://REDACTED.sh/i/a.png", "red"),
        ("https://catbox.moe/a.png", None),
        ("https://files.catbox.moe.example.com/a.png", None),
        ("https://notredacted.sh/i/a.png", None),
        ("not a url", None),
    ],
)
def test_an_image_url_is_known_by_its_hosts_domain(url: str, host: str | None) -> None:
    assert host_of_url(url) == host


def test_every_image_host_says_which_domain_its_images_are_on() -> None:
    for name in ("catbox", "imgbb", "imgbox", "oeimg", "ptscreens", "ra", "red"):
        assert salmon.images.HOSTS[name].IMAGE_DOMAINS, name


def test_a_cover_url_is_checked_for_each_tracker_of_the_run() -> None:
    assert url_refusal(COVER, ["RED", "OPS", "DIC"]) is None
    assert url_refusal(RED_IMAGE, ["RED", "OPS"]) is None
    assert url_refusal(RED_IMAGE, ["RED", "OPS", "DIC"]) == (
        f"{RED_IMAGE} is on red, which can't be used for DIC's images: its images only display on RED/OPS"
    )
    assert url_refusal("https://user@files.catbox.moe/a.png", ["RED"]) is not None


def test_spectral_urls_keep_the_order_given() -> None:
    assert parse_spectral_urls([f"2={FULL_2}", f" 1 = {FULL_1} , {ZOOM_1} "]) == (
        (2, (FULL_2,)),
        (1, (FULL_1, ZOOM_1)),
    )


def test_salmons_spectrals_from_an_earlier_run_stay_out_of_the_torrents(monkeypatch, tmp_path, dirs) -> None:
    _library, _downloads, torrents = dirs
    album = _album(tmp_path / "seeding" / "Album")
    (album / "Spectrals").mkdir()
    (album / "Spectrals" / "01 Full.png").write_bytes(b"png")

    run = _run_up(
        monkeypatch,
        album,
        torrents,
        args=("--spectral-url", f"1={FULL_1},{ZOOM_1}"),
        check_spectrals=_no_spectrals,
    )

    assert run.result.exit_code == 0, run.result.output
    torrent_lines = [line for upload in _uploads(run) for line in _torrent_lines(upload["file_input"][1])]
    assert torrent_lines
    assert not any("Spectrals" in line or ".png" in line for line in torrent_lines)


def test_spectral_urls_given_leave_the_spectrals_folder_alone(tmp_path) -> None:
    album = tmp_path / "Album"
    (album / "Spectrals").mkdir(parents=True)
    (album / "Spectrals" / "01 Full.png").write_bytes(b"png")
    uploads = SpectralUploads(str(album), ["RED"], given={1: [FULL_1]})

    assert anyio.run(uploads.urls_for, "RED", {1: "01 - one.flac"}) == {1: [FULL_1]}
    anyio.run(uploads.close)

    assert (album / "Spectrals" / "01 Full.png").exists()


def test_spectrals_bbcode_takes_a_track_with_its_full_image_only() -> None:
    bbcode = make_spectral_bbcode({1: "01 - one.flac"}, {1: [FULL_1]})

    assert bbcode == f"[hide=Spectrals][b]01 - one.flac Full[/b]\n[img={FULL_1}]\n\n[/hide]\n"


def test_a_cover_url_is_not_needed_for_an_existing_group(monkeypatch) -> None:
    """Into an existing group, as without --cover-url: the group has its cover."""

    async def no_download(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("looked for a cover")

    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", no_download)
    site: Any = SimpleNamespace(site_code="RED")

    # remove_downloaded: the group branch looks for no cover file.
    result = anyio.run(salmon.uploader.resolve_cover_url, site, 55, {}, "/album", None, True, COVER)

    assert result == (True, None)


def test_a_dry_run_names_no_cover_upload_with_a_cover_url(monkeypatch, tmp_path: Path, dirs) -> None:
    _library, _downloads, torrents = dirs

    run = _run_up(
        monkeypatch, _album(tmp_path / "seeding" / "Album"), torrents, args=("--cover-url", COVER, "--dry-run")
    )

    assert run.result.exit_code == 0, run.result.output
    assert "not uploading the cover" not in run.result.output
    assert run.tracker.not_gets() == []
