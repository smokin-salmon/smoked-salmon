"""without_tracking takes tracking off a store link, and keeps a link whose query may name the release."""

import asyncio
import re
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_tagger_metadata_source_url import make_release_data

from salmon.tagger import metadata as metadata_mod
from salmon.tagger import sources
from salmon.tagger.sources import without_tracking

DEEZER = "https://www.deezer.com/album/322064097"


@pytest.mark.parametrize(
    ("url", "published"),
    [
        (f"{DEEZER}?utm_source=newsletter#tracks", DEEZER),
        ("https://artist.bandcamp.com/album/some-album?from=fanpub", "https://artist.bandcamp.com/album/some-album"),
        # Bandcamp's pattern takes any host: off bandcamp.com, the query may be the release's ID.
        (
            "https://www.melon.com/album/detail.htm?albumId=10123456",
            "https://www.melon.com/album/detail.htm?albumId=10123456",
        ),
        ("https://music.example/album/some-album?from=x", "https://music.example/album/some-album?from=x"),
    ],
    ids=["store", "bandcamp", "melon", "bandcamp-own-domain"],
)
def test_a_store_link_loses_its_tracking_and_a_link_off_its_store_keeps_its_query(url, published) -> None:
    assert without_tracking(url) == published


def test_a_link_its_store_reads_only_with_its_query_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    scraper = SimpleNamespace(regex=re.compile(r"^https://store\.test/album\?id=\d+$"))
    monkeypatch.setattr(sources, "METASOURCES", {"Store": SimpleNamespace(Scraper=scraper)})

    assert without_tracking("https://store.test/album?id=5") == "https://store.test/album?id=5"


def test_a_link_whose_host_cannot_be_split_is_kept_as_given() -> None:
    # Bandcamp's pattern takes any host, so this is a store link, but urlsplit refuses the unclosed "[".
    url = "https://[bad/album/some-album?from=x"

    assert without_tracking(url) == url


def test_a_typed_store_link_is_taken_and_scraped_without_its_tracking(monkeypatch: pytest.MonkeyPatch) -> None:
    scraped: list[str] = []

    class DeezerScraper:
        regex = re.compile(r"^https://www\.deezer\.com/album/")

        async def scrape_release(self, url: str) -> dict:
            scraped.append(url)
            data = deepcopy(make_release_data())
            data["urls"] = [url]
            return data

    async def prompt(*_args, **_kwargs) -> str:
        return f"*{DEEZER}?utm_source=newsletter"

    monkeypatch.setattr(metadata_mod.click, "prompt", prompt)
    monkeypatch.setattr(metadata_mod, "METASOURCES", {"Deezer": SimpleNamespace(Scraper=DeezerScraper)})
    _metadata, source_url = asyncio.run(metadata_mod._select_choice({}, make_release_data()))

    assert source_url == DEEZER
    assert scraped == [DEEZER]
