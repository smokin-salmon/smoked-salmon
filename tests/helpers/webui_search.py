"""Helpers of tests/test_webui_search.py that other test modules use too."""

from typing import Any

import pytest

from salmon import ssrf
from salmon.errors import ScrapeError
from salmon.search import SEARCHSOURCES
from salmon.search.base import IdentData, SearchMixin


def _shown(artists: str, title: str, year: int) -> str:
    return SearchMixin.format_result(artists, title, f"{year} Label", track_count=10)


# What each fake store finds: a source's releases by ID, None when inactive, an exception when its search fails.
FOUND: dict[str, Any] = {
    "Bandcamp": {
        ("artist.bandcamp.com", "album", "the-album"): ("The Artist", "The Album", 2020, 10),
        ("artist.bandcamp.com", "track", "a-single"): ("The Artist", "A Single", 2021, 1),
    },
    "MusicBrainz": {},
    "Apple Music": ScrapeError("Apple Music changed its page"),
    "Discogs": None,
    "Beatport": {654321: ("The Artist", "The Album", 2020, 10)},
    "Qobuz": None,
    "Tidal": {("US", 987654): ("The Artist", "The Album (Deluxe)", 2020, 11)},
    "Deezer": {192837: ("The Artist", "The Album", 2020, None)},
}


def _fake_stores(monkeypatch: pytest.MonkeyPatch, found: dict[str, Any] = FOUND) -> list[tuple[str, str, int, bool]]:
    """Replace every store's search with one answering `found`; returns each search made: (source, query, limit,
    whether the guard was on)."""
    searches: list[tuple[str, str, int, bool]] = []
    for name, module in SEARCHSOURCES.items():

        async def search(_self: Any, searchstr: str, limit: int, name: str = name) -> tuple[str, Any]:
            searches.append((name, searchstr, limit, ssrf.active()))
            answer = found[name]
            if isinstance(answer, Exception):
                raise answer
            if answer is None:
                return name, None
            return name, {
                rls_id: (IdentData(artist, album, year, tracks, "WEB"), _shown(artist, album, year))
                for rls_id, (artist, album, year, tracks) in answer.items()
            }

        monkeypatch.setattr(module.Searcher, "search_releases", search)
    return searches
