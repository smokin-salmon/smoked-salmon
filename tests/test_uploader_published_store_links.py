"""A store link reaches a tracker without its query and fragment, which carry tracking and share IDs.

The run goes against the local fake tracker of helpers.uploader_dry_run and a fake image host, never a real one.
"""

import pytest
from helpers.uploader_dry_run import _album, _run_up
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

from salmon import cfg
from salmon.uploader.upload import generate_source_links

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

DEEZER = "https://www.deezer.com/album/322064097"
BANDCAMP = "https://artist.bandcamp.com/album/some-album"
NOT_A_STORE = "https://www.juno.co.uk/products/some-record/1094887-01/?ref=y"


def test_more_info_lists_each_store_link_once_without_its_tracking() -> None:
    links = generate_source_links(
        [f"{DEEZER}?utm_source=newsletter", f"{BANDCAMP}?from=fanpub#tracks", DEEZER, NOT_A_STORE]
    )

    assert links.count(f"[url={DEEZER}]") == 1
    assert f"[url={BANDCAMP}]" in links
    assert "utm_source" not in links and "fanpub" not in links and "#tracks" not in links
    # Not a store's link: its query may be what it points to.
    assert f"[url={NOT_A_STORE}]" in links


def test_a_source_url_with_tracking_reaches_the_tracker_without_it(tmp_path, monkeypatch) -> None:
    torrents = tmp_path / "torrents"
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])

    run = _run_up(
        monkeypatch, _album(tmp_path / "Album"), torrents, args=("--source-url", f"{DEEZER}?utm_source=newsletter")
    )

    assert run.result.exit_code == 0, run.result.output
    # Every form sent: the descriptions (the FLAC's and its conversions') and the lossy reports.
    sent = [value for request in run.tracker.sent for _name, value in request.fields if isinstance(value, str)]
    assert any(f"[url={DEEZER}]" in value for value in sent)
    assert any(f"Sourced from: {DEEZER}" in value for value in sent)
    assert not any("utm_source" in value for value in sent)
