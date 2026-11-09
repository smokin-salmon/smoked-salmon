"""A store link reaches a torrent description without its query and fragment, which carry tracking and share IDs."""

import pytest

from salmon import cfg
from salmon.uploader.upload import generate_source_links, generate_t_description

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


def test_the_source_line_shows_its_link_without_tracking_and_more_info_skips_its_other_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg.upload.description, "icons_in_descriptions", False)
    monkeypatch.setattr(cfg.upload.description, "include_tracklist_in_t_desc", True)

    description = generate_t_description(
        metadata={"date": "2025-07-25"},
        track_data={"01. Track.flac": {"duration": 321, "bit rate": 0, "precision": 24, "sample rate": 44100}},
        hybrid=False,
        metadata_urls=[DEEZER],
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=None,
        source_url=f"{DEEZER}?utm_source=newsletter",
    )

    assert f"[b]Source:[/b] [url={DEEZER}]Deezer[/url]" in description
    assert "utm_source" not in description
    assert "[b]More info:[/b]" not in description
