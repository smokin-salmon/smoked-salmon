"""Tidal's v2 API (issue #419), against a local fake server; never contacts Tidal."""

import asyncio
import time
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from aiohttp import web

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search.tidal import Searcher
from salmon.sources import tidal as tidal_source
from salmon.sources.tidal import MAX_PAGES, TidalBase, credentials_configured
from salmon.tagger.sources.tidal import Scraper

ALBUM_URL = "https://tidal.com/album/75194842"


def _track(track_id: str, title: str, artist_ids: list[str], isrc: str, version: str | None = None) -> dict:
    return {
        "id": track_id,
        "type": "tracks",
        "attributes": {
            "title": title,
            "version": version,
            "isrc": isrc,
            "explicit": False,
            "mediaTags": ["LOSSLESS", "HIRES_LOSSLESS"],
        },
        "relationships": {"artists": {"data": [{"id": a, "type": "artists"} for a in artist_ids]}},
    }


def _artist(artist_id: str, name: str) -> dict:
    return {"id": artist_id, "type": "artists", "attributes": {"name": name}}


def _item(track_id: str, disc: int, number: int) -> dict:
    return {"id": track_id, "type": "tracks", "meta": {"volumeNumber": disc, "trackNumber": number}}


# A trimmed /albums/{id}?include=artists,items,items.artists,coverArt document: two tracks on
# the first page of items, and a cursor to a second page.
ALBUM_DOC = {
    "data": {
        "id": "75194842",
        "type": "albums",
        "attributes": {
            "title": "Accept & Connect",
            "albumType": "EP",
            "barcodeId": "0617465881222",
            "releaseDate": "2018-03-02",
            "explicit": False,
            "numberOfItems": 3,
            "copyright": {"text": "2018 Majestic Casual Records"},
            "mediaTags": ["LOSSLESS"],
        },
        "relationships": {
            "artists": {"data": [{"id": "a1", "type": "artists"}]},
            "coverArt": {"data": [{"id": "art1", "type": "artworks"}]},
            "items": {
                "data": [_item("t1", 1, 1), _item("t2", 1, 2)],
                "links": {"self": "/albums/75194842/relationships/items", "meta": {"nextCursor": "page2"}},
            },
        },
    },
    "included": [
        _artist("a1", "Kordz"),
        _artist("a2", "Guest Singer"),
        _artist("a3", "Tiësto"),
        _track("t1", "Accept", ["a1", "a2"], "USUM71800001"),
        _track("t2", "Connect", ["a1", "a3"], "USUM71800002", version="Tiësto Remix"),
        {
            "id": "art1",
            "type": "artworks",
            "attributes": {
                "mediaType": "IMAGE",
                "files": [
                    {"href": "https://resources.tidal.com/images/a/b/80x80.jpg", "meta": {"width": 80}},
                    {"href": "https://resources.tidal.com/images/a/b/1280x1280.jpg", "meta": {"width": 1280}},
                ],
            },
        },
    ],
}

# The second page of items, whose track has an artist not seen on the first page.
ITEMS_PAGE_2 = {
    "data": [_item("t3", 2, 1)],
    "included": [_artist("a4", "Second Page Artist"), _track("t3", "Bonus", ["a1", "a4"], "USUM71800003")],
    "links": {"self": "/albums/75194842/relationships/items?page[cursor]=page2"},
}

SEARCH_DOC = {
    "data": [
        {
            "id": "kordz",
            "type": "searchResults",
            "relationships": {
                "albums": {"data": [{"id": "75194842", "type": "albums"}]},
                "tracks": {"data": []},
            },
        }
    ],
    "included": [
        _artist("a1", "Kordz"),
        {
            "id": "75194842",
            "type": "albums",
            "attributes": ALBUM_DOC["data"]["attributes"],
            "relationships": {"artists": {"data": [{"id": "a1", "type": "artists"}]}},
        },
    ],
}


class FakeTidal:
    """Record requests to a fake Tidal auth and API server."""

    def __init__(self) -> None:
        self.token_requests: list[dict] = []
        self.api_requests: list[web.Request] = []
        self.routes: dict[str, Callable[[web.Request], web.Response]] = {}
        self.runner: web.AppRunner | None = None

    async def _token(self, request: web.Request) -> web.Response:
        self.token_requests.append(dict(await request.post()))
        return web.json_response({"access_token": "fake-token", "token_type": "Bearer", "expires_in": 86400})

    async def _api(self, request: web.Request) -> web.Response:
        self.api_requests.append(request)
        handler = self.routes.get(request.match_info["path"])
        if handler is None:
            return web.json_response({"errors": [{"status": "404"}]}, status=404)
        return handler(request)

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/oauth2/token", self._token)
        app.router.add_get("/v2/{path:.*}", self._api)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


@pytest.fixture
def tidal(monkeypatch: pytest.MonkeyPatch) -> FakeTidal:
    """Point every Tidal client at a fake server, with client credentials and a cold token cache."""
    monkeypatch.setattr(cfg.metadata.tidal, "client_id", "an-id")
    monkeypatch.setattr(cfg.metadata.tidal, "client_secret", "a-secret")
    monkeypatch.setattr(TidalBase, "_access_token", None)
    monkeypatch.setattr(TidalBase, "_token_expiry", 0.0)
    return FakeTidal()


def _run(fake: FakeTidal, monkeypatch: pytest.MonkeyPatch, body: Callable[[], Any]) -> Any:
    async def main() -> Any:
        base = await fake.start()
        monkeypatch.setattr(TidalBase, "url", f"{base}/v2")
        monkeypatch.setattr(TidalBase, "token_url", f"{base}/v1/oauth2/token")
        try:
            return await body()
        finally:
            await fake.stop()

    return anyio.run(main)


def _json(doc: dict, status: int = 200, headers: dict | None = None) -> Callable[[web.Request], web.Response]:
    return lambda request: web.json_response(doc, status=status, headers=headers)


def test_one_client_credentials_token_serves_concurrent_and_later_requests(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch
) -> None:
    tidal.routes["ping"] = _json({"data": []})

    async def body() -> None:
        # A cold cache hit by a search over several regions at once, then more requests.
        await asyncio.gather(*(Searcher().get_json("/ping") for _ in range(5)))
        await Scraper().get_json("/ping")

    _run(tidal, monkeypatch, body)
    assert tidal.token_requests == [
        {"grant_type": "client_credentials", "client_id": "an-id", "client_secret": "a-secret"}
    ]
    assert len(tidal.api_requests) == 6
    assert {r.headers["Authorization"] for r in tidal.api_requests} == {"Bearer fake-token"}
    assert {r.headers["Accept"] for r in tidal.api_requests} == {"application/vnd.api+json"}


def test_album_is_parsed_into_salmon_metadata_across_item_pages(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch
) -> None:
    tidal.routes["albums/75194842"] = _json(ALBUM_DOC)
    tidal.routes["albums/75194842/relationships/items"] = _json(ITEMS_PAGE_2)

    data = _run(tidal, monkeypatch, lambda: Scraper().scrape_release(ALBUM_URL))

    album_request, page_request = tidal.api_requests
    assert album_request.query["include"] == "artists,items,items.artists,coverArt"
    assert page_request.query["page[cursor]"] == "page2"
    assert "items.artists" in page_request.query["include"].split(",")

    assert data["title"] == "Accept & Connect"
    assert data["date"] == "2018-03-02"
    assert data["year"] == 2018
    assert data["label"] == "Majestic Casual Records"
    assert data["upc"] == "0617465881222"
    assert data["cover"] == "https://resources.tidal.com/images/a/b/1280x1280.jpg"
    assert ("Kordz", "main") in data["artists"]

    tracks = data["tracks"]
    assert sorted(tracks) == ["1", "2"]
    accept, connect, bonus = tracks["1"]["1"], tracks["1"]["2"], tracks["2"]["1"]
    assert (accept["title"], accept["isrc"], accept["explicit"]) == ("Accept", "USUM71800001", False)
    assert accept["format"] == "HI_RES"
    assert accept["artists"] == [("Kordz", "main"), ("Guest Singer", "guest")]
    assert ("Tiësto", "remixer") in connect["artists"]
    # The second page's track keeps its artists.
    assert bonus["artists"] == [("Kordz", "main"), ("Second Page Artist", "guest")]


def test_item_paging_stops_at_its_cap(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["albums/75194842"] = _json(ALBUM_DOC)
    # A cursor that never runs out.
    tidal.routes["albums/75194842/relationships/items"] = _json(
        {"data": [], "links": {"self": "x", "meta": {"nextCursor": "again"}}}
    )

    with pytest.raises(ScrapeError):
        _run(tidal, monkeypatch, lambda: Scraper().fetch_data(ALBUM_URL))
    assert len(tidal.api_requests) == 1 + MAX_PAGES


def test_rate_limited_request_waits_for_retry_after(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter([_json({}, 429, {"Retry-After": "1"}), _json({"data": []})])
    tidal.routes["ping"] = lambda request: next(responses)(request)

    async def body() -> tuple[dict, float]:
        start = time.monotonic()
        result = await Scraper().get_json("/ping")
        return result, time.monotonic() - start

    result, elapsed = _run(tidal, monkeypatch, body)
    assert result == {"data": []}
    assert len(tidal.api_requests) == 2
    assert elapsed >= 1


def test_rate_limit_retries_are_bounded(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["ping"] = _json({}, 429, {"Retry-After": "0"})

    with pytest.raises(ScrapeError):
        _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert len(tidal.api_requests) == 1 + tidal_source.RATE_LIMIT_RETRIES


def test_rate_limit_asking_for_a_long_wait_is_not_retried(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["ping"] = _json({}, 429, {"Retry-After": "3600"})

    with pytest.raises(ScrapeError):
        _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert len(tidal.api_requests) == 1


def test_search_uses_the_search_results_collection(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["searchResults"] = _json(SEARCH_DOC)

    source, releases = _run(tidal, monkeypatch, lambda: Searcher().search_releases("kordz accept", 5))

    assert source == "Tidal"
    assert all(r.query["filter[query]"] == "kordz accept" for r in tidal.api_requests)
    # One release, found in every configured region and deduplicated.
    ((cc, rls_id),) = releases
    assert rls_id == "75194842"
    ident = releases[(cc, rls_id)][0]
    assert (ident.artist, ident.album, ident.year, ident.track_count) == ("Kordz", "Accept & Connect", 2018, 3)


@pytest.mark.parametrize(
    ("token", "notified"),
    [("a-token-from-the-web-player", True), ("your-token", False), (None, False)],
)
def test_retired_token_alone_leaves_tidal_off_with_one_notice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], token: str | None, notified: bool
) -> None:
    monkeypatch.setattr(cfg.metadata.tidal, "client_id", None)
    monkeypatch.setattr(cfg.metadata.tidal, "client_secret", None)
    monkeypatch.setattr(cfg.metadata.tidal, "token", token)
    tidal_source._notify_retired_token.cache_clear()

    assert credentials_configured() is False
    assert credentials_configured() is False

    out = capsys.readouterr().out
    assert out.count("developer.tidal.com") == (1 if notified else 0)
