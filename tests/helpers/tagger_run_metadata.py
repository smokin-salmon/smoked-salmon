"""Helpers of tests/test_tagger_run_metadata.py that other test modules use too."""

import pytest
from aiohttp import web

from helpers.ssrf import LOOPBACK, FakeStore
from salmon import ssrf
from salmon.tagger.sources import discogs

RELEASE_URL = "https://www.discogs.com/release/2520452-Various-Dance-Anthems"


# Discogs release 2520452, trimmed to the credits of track 3-3, which names its artist differently from Discogs.
RELEASE = {
    "title": "Dance Anthems",
    "artists": [{"name": "Various", "anv": "", "join": ""}],
    "genres": ["Electronic"],
    "year": 1999,
    "released": "1999-05-03",
    "formats": [{"name": "CD", "descriptions": ["Compilation"]}],
    "labels": [{"name": "Some Label", "catno": "SL 1"}],
    "tracklist": [
        {
            "position": "3-3",
            "type_": "track",
            "title": "Tell Me It's Real",
            "artists": [{"name": "K-Ci & JoJo", "anv": "K-Ci And JoJo", "join": ""}],
            "extraartists": [{"name": "Club Asylum", "anv": "", "role": "Remix"}],
        }
    ],
}


class FakeDiscogs(FakeStore):
    """The Discogs API: release 2520452, whatever is asked."""

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request.path)
        return web.json_response(RELEASE)


def pointed_at(monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)
    monkeypatch.setattr(discogs.Scraper, "url", store.url(""))


# What a store that states its credits hands the merge for track 3-3: K-Ci & JoJo as main, and as a guest it only
# guessed.
STATED = [("K-Ci & JoJo", "guest"), ("K-Ci & JoJo", "main"), ("Club Asylum", "remixer")]
GUESSED = [("K-Ci & JoJo", "guest")]


def stating_credits(monkeypatch: pytest.MonkeyPatch, states: bool = True) -> None:
    """Make the fake's Discogs a store that states its credits (or, with `states` false, one that gives the same
    credits and states nothing), with one guess on track 3-3."""
    parse_tracks = discogs.Scraper.parse_tracks

    async def parsed(self: discogs.Scraper, soup: dict) -> dict:
        tracks = await parse_tracks(self, soup)
        tracks["3"]["3"]["artists"] = list(STATED)
        tracks["3"]["3"]["assumed_artists"] = list(GUESSED)
        return tracks

    monkeypatch.setattr(discogs.Scraper, "parse_tracks", parsed)
    monkeypatch.setattr(discogs.Scraper, "states_credits", states, raising=False)
