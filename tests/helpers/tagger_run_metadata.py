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
