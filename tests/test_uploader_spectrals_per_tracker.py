"""Spectrals go to each tracker's own specs host, uploaded once per host (#535).

The runs here go against the local fake tracker of test_uploader_dry_run and fake image hosts, never a real one.
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    RENAMED,
    _album,
    _lossy_with_spectrals,
    _printed_uploads,
    _run_up,
    _torrent_lines,
)

import salmon.images
import salmon.trackers
import salmon.uploader
from salmon import cfg, dryrun
from salmon.config.validations import ImageUploader, TrackerImageSettings
from salmon.images.base import BaseImageUploader
from salmon.uploader import spectrals as spectrals_module
from salmon.uploader.spectrals import SpectralUploads, get_spectrals_path, specs_hosts_text

SPECTRALS = ["01 Full.png", "01 Zoom.png"]


@pytest.fixture(autouse=True)
def red_and_ops(monkeypatch) -> None:
    """RED and OPS are the configured trackers, so a run's spectrals may be for either."""
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
def hosts(monkeypatch) -> list[tuple[str, str, str]]:
    """Make catbox and imgbox fake hosts, and give the (host, file name, URL) of each image they take.

    An upload reads the file, so it fails if the file is gone.
    """
    uploads: list[tuple[str, str, str]] = []

    def host(name: str) -> SimpleNamespace:
        class ImageUploader(BaseImageUploader):
            async def upload_file(self, filename: str) -> tuple[str, None]:
                Path(filename).read_bytes()
                url = f"https://{name}.test/{len(uploads) + 1}.png"
                uploads.append((name, os.path.basename(filename), url))
                return url, None

        return SimpleNamespace(ImageUploader=ImageUploader)

    monkeypatch.setitem(salmon.images.HOSTS, "catbox", host("catbox"))
    monkeypatch.setitem(salmon.images.HOSTS, "imgbox", host("imgbox"))
    return uploads


@pytest.fixture
def scratch(monkeypatch, tmp_path) -> Path:
    """The system temporary directory, where a run may keep spectrals it still needs: empty, and watched."""
    folder = tmp_path / "system-tmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def _image_settings(monkeypatch, **specs_hosts: str) -> None:
    """Covers and spectrals on catbox, but for the trackers given a specs host of their own, e.g. red="imgbox"."""
    overrides: dict[str, Any] = {code: TrackerImageSettings(specs_uploader=host) for code, host in specs_hosts.items()}  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr(cfg, "image", ImageUploader(cover_uploader="catbox", specs_uploader="catbox", **overrides))


def _spectral_uploads(hosts: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The (host, file name) of each spectral image uploaded, hosts in the order of their uploads."""
    by_host: dict[str, list[str]] = {}
    for host, name, _url in hosts:
        if name in SPECTRALS:
            by_host.setdefault(host, []).append(name)
    return [(host, name) for host, names in by_host.items() for name in sorted(names)]


def _spectral_urls(hosts: list[tuple[str, str, str]], host: str) -> list[str]:
    return [url for name_host, name, url in hosts if name_host == host and name in SPECTRALS]


def _uploads(run) -> list[dict[str, Any]]:
    """The form of each torrent the run uploaded, in order: the FLAC and its two transcodes, per tracker."""
    return [dict(sent.fields) for sent in run.tracker.sent if sent.query.get("action") == "upload"]


def _reports(run) -> list[str]:
    """The lossy master report sent with each torrent, in order."""
    return [dict(sent.fields)["extra"] for sent in run.tracker.sent if sent.path == "/reportsv2.php"]


def _has_all(text: str, urls: list[str]) -> bool:
    return bool(urls) and all(url in text for url in urls)


def _has_any(text: str, urls: list[str]) -> bool:
    return any(url in text for url in urls)


def _album_listings(monkeypatch) -> list[list[str]]:
    """Record what the album folder holds each time a torrent is uploaded from it."""
    listings: list[list[str]] = []
    upload_and_report = salmon.uploader.upload_and_report

    async def recording(gazelle_site, path, *args, **kwargs) -> Any:
        listings.append(sorted(os.listdir(path)))
        return await upload_and_report(gazelle_site, path, *args, **kwargs)

    monkeypatch.setattr(salmon.uploader, "upload_and_report", recording)
    return listings


def _left_behind(downloads: Path, scratch: Path) -> list[Path]:
    """Spectral files the run left in the album it uploaded or in the system temporary directory."""
    return [*(downloads / RENAMED).rglob("*.png"), *scratch.rglob("*")]


def test_without_per_tracker_hosts_the_spectrals_are_uploaded_once_and_deleted_before_the_first_upload(
    monkeypatch, tmp_path, dirs, hosts, scratch
) -> None:
    _library, downloads, torrents = dirs
    _image_settings(monkeypatch)
    listings = _album_listings(monkeypatch)

    run = _run_up(monkeypatch, _album(tmp_path / "seeding" / "Album"), torrents)

    assert run.result.exit_code == 0, run.result.output
    assert _spectral_uploads(hosts) == [("catbox", name) for name in SPECTRALS]
    urls = _spectral_urls(hosts, "catbox")
    uploads = _uploads(run)
    assert len(uploads) == 6
    assert _has_all(uploads[0]["release_desc"], urls) and _has_all(uploads[3]["release_desc"], urls)
    assert all(_has_all(report, urls) for report in _reports(run))
    # Deleted right after that one upload, as before per-tracker hosts: gone before the first torrent is made.
    assert len(listings) == 6 and all("Spectrals" not in listing for listing in listings)
    assert _left_behind(downloads, scratch) == []


def test_each_tracker_gets_the_spectrals_of_its_own_host_uploaded_once_per_host(
    monkeypatch, tmp_path, dirs, hosts, scratch
) -> None:
    _library, downloads, torrents = dirs
    _image_settings(monkeypatch, red="imgbox")
    listings = _album_listings(monkeypatch)
    prompted_hosts: list[str | None] = []

    async def check_spectrals(path: str, *args: Any, hosts: str | None = None, **kwargs: Any) -> Any:
        prompted_hosts.append(hosts)
        return await _lossy_with_spectrals(path, *args, **kwargs)

    run = _run_up(monkeypatch, _album(tmp_path / "seeding" / "Album"), torrents, check_spectrals=check_spectrals)

    assert run.result.exit_code == 0, run.result.output
    # The spectral IDs prompt names where they go.
    assert prompted_hosts == ["imgbox (RED), catbox (OPS)"]
    # RED's host before the RED upload, OPS's once the run moves on to OPS.
    assert _spectral_uploads(hosts) == [("imgbox", name) for name in SPECTRALS] + [
        ("catbox", name) for name in SPECTRALS
    ]
    red_urls, ops_urls = _spectral_urls(hosts, "imgbox"), _spectral_urls(hosts, "catbox")
    uploads, reports = _uploads(run), _reports(run)
    assert len(uploads) == len(reports) == 6
    # Each FLAC's description, and the lossy master report of the FLAC and of both transcodes, to each tracker.
    for texts, urls, others in (
        ([uploads[0]["release_desc"], *reports[:3]], red_urls, ops_urls),
        ([uploads[3]["release_desc"], *reports[3:]], ops_urls, red_urls),
    ):
        for text in texts:
            assert _has_all(text, urls)
            assert not _has_any(text, others)
    # Kept for OPS, but never in the folder the torrents are made from, and deleted once the run is over.
    assert len(listings) == 6 and all("Spectrals" not in listing for listing in listings)
    torrent_lines = [line for upload in uploads for line in _torrent_lines(upload["file_input"][1])]
    assert not any("Spectrals" in line or ".png" in line for line in torrent_lines)
    assert _left_behind(downloads, scratch) == []


def test_trackers_sharing_a_host_share_one_upload(monkeypatch, tmp_path, dirs, hosts, scratch) -> None:
    _library, downloads, torrents = dirs
    _image_settings(monkeypatch, red="imgbox", ops="imgbox")
    listings = _album_listings(monkeypatch)

    run = _run_up(monkeypatch, _album(tmp_path / "seeding" / "Album"), torrents)

    assert run.result.exit_code == 0, run.result.output
    assert _spectral_uploads(hosts) == [("imgbox", name) for name in SPECTRALS]
    urls = _spectral_urls(hosts, "imgbox")
    assert all(_has_all(report, urls) for report in _reports(run))
    # No other host to upload to: deleted right after the one upload, as with a single host.
    assert len(listings) == 6 and all("Spectrals" not in listing for listing in listings)
    assert _left_behind(downloads, scratch) == []


def test_the_spectrals_kept_for_later_trackers_are_deleted_when_an_upload_raises(
    monkeypatch, tmp_path, dirs, hosts, scratch
) -> None:
    _library, downloads, torrents = dirs
    _image_settings(monkeypatch, red="imgbox")
    kept: list[str] = []

    async def failing(*_args: Any, **_kwargs: Any) -> Any:
        kept.extend([str(path.relative_to(scratch)) for path in scratch.rglob("*.png")])
        raise RuntimeError("the upload broke")

    run = _run_up(monkeypatch, _album(tmp_path / "seeding" / "Album"), torrents, upload_and_report=failing)

    assert isinstance(run.result.exception, RuntimeError)
    # Uploaded for RED, and kept outside the album for OPS until the error ended the run.
    assert _spectral_uploads(hosts) == [("imgbox", name) for name in SPECTRALS]
    assert sorted(os.path.basename(path) for path in kept) == SPECTRALS
    assert _left_behind(downloads, scratch) == []


def test_a_dry_run_stands_in_one_set_of_urls_per_host(monkeypatch, dirs, hosts, scratch) -> None:
    library, _downloads, torrents = dirs
    _image_settings(monkeypatch, red="imgbox")

    run = _run_up(monkeypatch, _album(library / "Album"), torrents, args=("--dry-run",))

    assert run.result.exit_code == 0, run.result.output
    assert hosts == []
    output = run.result.output
    assert output.count("Dry run: not uploading the spectrals of 1 track(s) to imgbox.") == 1
    assert output.count("Dry run: not uploading the spectrals of 1 track(s) to catbox.") == 1
    printed = [dict(fields)["release_desc"] for _line, fields, _torrent in _printed_uploads(output)]
    assert len(printed) == 6
    for description, host, other in ((printed[0], "imgbox", "catbox"), (printed[3], "catbox", "imgbox")):
        assert _has_all(description, [dryrun.image_url(name, host) for name in SPECTRALS])
        assert not _has_any(description, [dryrun.image_url(name, other) for name in SPECTRALS])
    assert list(scratch.iterdir()) == []


def test_spectrals_checked_after_the_first_upload_go_to_each_trackers_host(
    monkeypatch, tmp_path, dirs, hosts, scratch
) -> None:
    _library, downloads, torrents = dirs
    _image_settings(monkeypatch, red="imgbox")
    # The check after the upload is the spectrals module's own.
    monkeypatch.setattr(spectrals_module, "check_spectrals", _lossy_with_spectrals)
    listings = _album_listings(monkeypatch)

    # The check asks for the lossy master comment before RED's spectrals go up: the empty answer is no comment.
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "seeding" / "Album"),
        torrents,
        args=("--spectrals-after",),
        input="\n\nOPS\n\n",
    )

    assert run.result.exit_code == 0, run.result.output
    assert _spectral_uploads(hosts) == [("imgbox", name) for name in SPECTRALS] + [
        ("catbox", name) for name in SPECTRALS
    ]
    red_urls, ops_urls = _spectral_urls(hosts, "imgbox"), _spectral_urls(hosts, "catbox")
    uploads, reports = _uploads(run), _reports(run)
    # RED's FLAC went up before the check; OPS's FLAC carries OPS's URLs.
    assert not _has_any(uploads[0]["release_desc"], red_urls + ops_urls)
    assert _has_all(uploads[3]["release_desc"], ops_urls)
    assert not _has_any(uploads[3]["release_desc"], red_urls)
    # The check runs right after RED's FLAC: its report, and those of RED's transcodes, have RED's URLs; OPS's
    # have OPS's.
    assert len(reports) == 6
    assert all(_has_all(report, red_urls) and not _has_any(report, ops_urls) for report in reports[:3])
    assert all(_has_all(report, ops_urls) and not _has_any(report, red_urls) for report in reports[3:])
    assert len(listings) == 6 and all("Spectrals" not in listing for listing in listings)
    assert _left_behind(downloads, scratch) == []


def test_the_prompt_names_each_host_with_its_trackers_only_when_they_differ(monkeypatch) -> None:
    _image_settings(monkeypatch)
    assert specs_hosts_text(["RED", "OPS", "DIC"]) == "catbox"
    _image_settings(monkeypatch, red="imgbox")
    assert specs_hosts_text(["RED", "OPS", "DIC"]) == "imgbox (RED), catbox (OPS/DIC)"
    assert specs_hosts_text(["OPS"]) == "catbox"


@pytest.mark.parametrize(
    ("multi_tracker_upload", "flac_group", "expected"),
    [(True, None, ["RED", "OPS"]), (False, None, ["RED"]), (True, {"group": {}}, ["RED"])],
)
def test_only_the_trackers_a_run_can_reach_count(
    monkeypatch, multi_tracker_upload: bool, flac_group: dict[str, Any] | None, expected: list[str]
) -> None:
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", multi_tracker_upload)
    assert salmon.uploader._trackers_for_run("RED", flac_group) == expected


def test_a_run_that_can_only_reach_one_host_deletes_the_spectrals_after_its_upload(
    monkeypatch, tmp_path, hosts, scratch
) -> None:
    _image_settings(monkeypatch, ops="imgbox")
    album = tmp_path / "Album"
    spectrals = Path(get_spectrals_path(str(album)))
    spectrals.mkdir(parents=True)
    for name in SPECTRALS:
        (spectrals / name).write_bytes(b"png")

    async def run() -> Any:
        return await SpectralUploads(str(album), ["RED"]).urls_for("RED", {1: "01. one.flac"})

    assert anyio.run(run) == {1: _spectral_urls(hosts, "catbox")}
    assert not spectrals.exists()
    assert list(scratch.iterdir()) == []


def _entries(album: Path) -> list[str]:
    """What the album holds. Its tracks are retagged in place before the rename, so only the names count."""
    return [str(entry.relative_to(album)) for entry in sorted(album.rglob("*"))]


@pytest.mark.parametrize("with_tmp_dir", [False, True])
def test_a_renamed_album_leaves_no_spectrals_in_the_source_or_tmp_dir(
    monkeypatch, tmp_path, dirs, hosts, scratch, with_tmp_dir: bool
) -> None:
    # #569 and #573: the spectrals are made under the old name, the folder is renamed, and none may stay behind.
    _library, downloads, torrents = dirs
    _image_settings(monkeypatch)
    tmp_dir = tmp_path / "salmon-tmp"
    if with_tmp_dir:
        tmp_dir.mkdir()
        monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    album = _album(tmp_path / "seeding" / "Album")
    before = _entries(album)

    run = _run_up(monkeypatch, album, torrents)

    assert run.result.exit_code == 0, run.result.output
    assert _spectral_uploads(hosts) == [("catbox", name) for name in SPECTRALS]
    assert (downloads / RENAMED).is_dir()
    assert _left_behind(downloads, scratch) == []
    assert not (album / "Spectrals").exists()
    assert _entries(album) == before
    if with_tmp_dir:
        assert list(tmp_dir.iterdir()) == []
