"""salmon web's store search and metadata lookup, and the guard on what a web job fetches (#656, salmon.ssrf).

The searches run fake stores: each store's search is replaced, so nothing is sent. The lookups and the upload jobs
fetch from a local fake store on 127.0.0.1, which the guard refuses unless a test lets loopback through: the
network guard in conftest.py stands.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from aiohttp.test_utils import TestClient
from test_ssrf import LOOPBACK, FakeStore, bandcamp_page  # pyright: ignore[reportMissingImports]
from test_uploader_dry_run import (  # noqa: F401  # pyright: ignore[reportMissingImports]
    _album,
    fake_upload_world,
    image_uploads,
)
from test_webui_jobs import AUTH, _log, _with_app  # pyright: ignore[reportMissingImports]
from test_webui_upload import _drive, _start, _web_run, roots  # noqa: F401  # pyright: ignore[reportMissingImports]

import salmon.search
import salmon.tagger.metadata
import salmon.uploader
from salmon import cfg, ssrf
from salmon.config.validations import ProxyCfg
from salmon.errors import ScrapeError
from salmon.search import SEARCHSOURCES, metas
from salmon.search.base import IdentData, SearchMixin
from salmon.trackers import base
from salmon.webui.jobs import JobManager

pytestmark = pytest.mark.usefixtures("image_uploads")

# Secrets the config holds for the stores, planted where a store's answer could repeat them.
PLANTED = {
    "qobuz": "planted-qobuz-user-token-5b1e",
    "tidal_secret": "planted-tidal-client-secret-c07a",
    "tidal_token": "planted-tidal-token-8e42",
    "discogs": "planted-discogs-token-a9d0",
    "beatport": "planted-beatport-password-36f1",
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_learned_secrets", set())
    monkeypatch.setattr(cfg, "proxy", ProxyCfg())


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


async def _search(client: TestClient, **query: Any) -> Any:
    response = await client.get("/api/search", params=query, headers=AUTH)
    assert response.status == 200, await response.text()
    return await response.json()


# --- Search -----------------------------------------------------------------------------------------


def test_a_search_finds_what_salmon_metas_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    searches = _fake_stores(monkeypatch)
    assert metas.callback is not None
    anyio.run(metas.callback, ("the", "album"), 10, cfg.upload.search.limit)
    cli = click.unstyle(capsys.readouterr().out)
    cli_searches = list(searches)
    searches.clear()

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await _search(client, q="the album", track_count="10")
        assert answer["query"] == "the album"
        sources = answer["sources"]
        assert [source["name"] for source in sources] == list(SEARCHSOURCES)
        # Each release the command prints, with the same text and URL.
        web_lines = [f"> {rls['summary']} {rls['url']}" for source in sources for rls in source["releases"]]
        cli_lines = [line for line in cli.splitlines() if line.startswith("> ")]
        assert web_lines == cli_lines
        assert "https://artist.bandcamp.com/album/the-album" in cli
        statuses = {source["name"]: source["status"] for source in sources}
        assert statuses == {
            "Bandcamp": "found",
            "MusicBrainz": "none",
            "Apple Music": "failed",
            "Discogs": "inactive",
            "Beatport": "found",
            "Qobuz": "inactive",
            "Tidal": "found",
            "Deezer": "found",
        }
        for name, status in statuses.items():
            said = {
                "none": f"No results found from {name}.",
                "inactive": f"{name} is inactive.",
                "failed": "Failed to scrape",
            }.get(status)
            assert said is None or said in cli
        # The single (1 track) filtered out by the track count, as by the command.
        assert [rls["album"] for rls in sources[0]["releases"]] == ["The Album"]
        assert sources[0]["releases"][0] == {
            "artist": "The Artist",
            "album": "The Album",
            "year": 2020,
            "track_count": 10,
            "summary": "The Artist - The Album {Tracks: 10} 2020 Label",
            "url": "https://artist.bandcamp.com/album/the-album",
        }

    _with_app(test)
    # The same sources, query and limit; in salmon web behind the guard.
    assert [(name, query, limit) for name, query, limit, _guarded in searches] == [
        (name, query, limit) for name, query, limit, _guarded in cli_searches
    ]
    assert {limit for _name, _query, limit, _guarded in searches} == {cfg.upload.search.limit}
    assert all(guarded for *_rest, guarded in searches)
    assert not any(guarded for *_rest, guarded in cli_searches)


@pytest.mark.parametrize(
    "query",
    [{}, {"q": "  "}, {"q": "x" * 201}, {"q": "album", "track_count": "ten"}, {"q": "album", "track_count": "0"}],
)
def test_a_search_without_a_usable_query_is_refused_before_any_store_is_asked(
    monkeypatch: pytest.MonkeyPatch, query: dict[str, str]
) -> None:
    searches = _fake_stores(monkeypatch)

    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await client.get("/api/search", params=query, headers=AUTH)
        assert response.status == 422

    _with_app(test)
    assert searches == []


def test_searches_and_lookups_run_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    running: list[int] = [0]
    most: list[int] = [0]

    async def search(_self: Any, _searchstr: str, _limit: int) -> tuple[str, Any]:
        running[0] += 1
        most[0] = max(most[0], running[0])
        await asyncio.sleep(0.05)
        running[0] -= 1
        return "Bandcamp", {}

    for module in SEARCHSOURCES.values():
        monkeypatch.setattr(module.Searcher, "search_releases", search)

    async def test(client: TestClient, _manager: JobManager) -> None:
        await asyncio.gather(_search(client, q="one"), _search(client, q="two"))

    _with_app(test)
    # One search asks every store at once; the second waits for it.
    assert most[0] == len(SEARCHSOURCES)


def test_a_search_requires_the_login(monkeypatch: pytest.MonkeyPatch) -> None:
    searches = _fake_stores(monkeypatch)

    async def test(client: TestClient, _manager: JobManager) -> None:
        assert (await client.get("/api/search", params={"q": "album"})).status == 401
        assert (await client.get("/api/metadata", params={"url": "https://bandcamp.com/album/x"})).status == 401

    _with_app(test)
    assert searches == []


# --- Metadata lookup --------------------------------------------------------------------------------


def _lookup_run(test: Any, store: FakeStore | None = None) -> FakeStore:
    store = store or FakeStore()

    async def serving(client: TestClient, manager: JobManager) -> None:
        async with store.serving():
            await test(client, store)

    _with_app(serving)
    return store


def test_a_lookup_gives_what_salmon_meta_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)

    async def test(client: TestClient, store: FakeStore) -> None:
        url = store.url("/album/x")
        response = await client.get("/api/metadata", params={"url": url}, headers=AUTH)
        assert response.status == 200, await response.text()
        answer = await response.json()
        assert (answer["url"], answer["source"]) == (url, "Bandcamp")
        metadata = answer["metadata"]
        assert (metadata["title"], metadata["date"]) == ("The Album", "2022-03-04")
        assert metadata["tracks"]["1"]["1"]["title"] == "The Album"
        # What salmon meta leaves out.
        assert not {"encoding", "media", "encoding_vbr", "source"} & set(metadata)
        assert store.requests == ["/album/x"]

    _lookup_run(test)


@pytest.mark.parametrize("path", ["/nothing", "/release/x", ""])
def test_a_lookup_no_scraper_takes_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)

    async def test(client: TestClient, store: FakeStore) -> None:
        url = store.url(path) if path else ""
        response = await client.get("/api/metadata", params={"url": url}, headers=AUTH)
        assert response.status == 422
        assert store.requests == []

    _lookup_run(test)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_a_lookup_of_an_internal_address_is_refused_at_the_connection(host: str) -> None:
    async def test(client: TestClient, store: FakeStore) -> None:
        response = await client.get("/api/metadata", params={"url": store.url("/album/x", host)}, headers=AUTH)
        assert response.status == 502
        detail = (await response.json())["detail"]
        assert f"Refused to connect to {host}" in detail
        assert store.requests == []

    _lookup_run(test)


# --- The metadata question and the cover of an upload job ---------------------------------------------


async def _asks_for_urls(_path: str, _tags: Any, rls_data: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """The metadata question, as get_metadata asks it when no store finds the release."""
    return await salmon.tagger.metadata._select_choice({}, dict(rls_data))


def test_a_pasted_url_to_an_internal_address_is_refused_in_an_upload_job(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],  # noqa: F811
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    tracker, _queued, _logins = fake_upload_world(
        monkeypatch, torrents, multi_tracker_upload=False, get_metadata=_asks_for_urls
    )
    store = FakeStore()

    def answer(question: dict[str, Any]) -> str:
        if "paste URLs" in question["text"]:
            return store.url(f"/album/x?token={PLANTED['qobuz']}")
        return ""

    async def test(client: TestClient, manager: JobManager) -> None:
        async with store.serving():
            job, asked = await _drive(
                client, manager, await _start(client, album, params={"trackers": ["RED"]}), answer
            )
        assert any("paste URLs" in question["text"] for question in asked)
        # As any failed scrape of a pasted URL ends the run.
        assert job.status == "failed"
        assert "Refused to connect to 127.0.0.1" in str(job.error)
        assert PLANTED["qobuz"] not in json.dumps(job.detail())

    _web_run(tracker, test)
    assert store.requests == []
    assert tracker.not_gets() == []


def test_a_cover_url_to_an_internal_address_is_refused_in_an_upload_job(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],  # noqa: F811
) -> None:
    downloads, _library, torrents = roots
    album = _album(downloads / "Album")
    # No cover in the folder: the run downloads the one the store gives.
    (album / "cover.jpg").unlink()
    store = FakeStore()

    async def reviewed(metadata: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return metadata

    tracker, _queued, _logins = fake_upload_world(
        monkeypatch, torrents, multi_tracker_upload=False, review_metadata_with_ai=reviewed
    )
    stub = salmon.uploader.get_metadata

    async def test(client: TestClient, manager: JobManager) -> None:
        async with store.serving():
            metadata, _source = await stub(str(album), {}, {})

            async def scraped(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], None]:
                # A store page's cover URL, pointing inward.
                return {**metadata, "cover": store.url("/cover.png")}, None

            monkeypatch.setattr(salmon.uploader, "get_metadata", scraped)
            job, _asked = await _drive(
                client, manager, await _start(client, album, params={"trackers": ["RED"]}, dry_run=True)
            )
        assert job.status == "done", (job.error, _log(job))
        assert any(
            "Failed to download cover image (ERROR Refused to connect to 127.0.0.1" in line for line in _log(job)
        )

    _web_run(tracker, test)
    assert store.requests == []


# --- Secrets ----------------------------------------------------------------------------------------


def _plant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", PLANTED["qobuz"])
    monkeypatch.setattr(cfg.metadata.tidal, "client_secret", PLANTED["tidal_secret"])
    monkeypatch.setattr(cfg.metadata.tidal, "token", PLANTED["tidal_token"])
    monkeypatch.setattr(cfg.metadata, "discogs_token", PLANTED["discogs"])
    monkeypatch.setattr(cfg.metadata.beatport, "password", PLANTED["beatport"])


def test_no_store_secret_leaves_in_a_search_or_a_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    _plant(monkeypatch)
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)
    secrets = " ".join(PLANTED.values())
    # Every field a store answers with repeats them.
    _fake_stores(
        monkeypatch,
        {
            name: {(f"{name}.example", "album", secrets): (secrets, f"Album {secrets}", 2020, 10)}
            if name == "Bandcamp"
            else {f"{name} {secrets}": (secrets, f"Album {secrets}", 2020, 10)}
            for name in SEARCHSOURCES
        },
    )
    store = FakeStore(bandcamp_page(cover_url=f"https://covers.example/{secrets}.jpg").replace("The Album", secrets))

    async def test(client: TestClient, store: FakeStore) -> None:
        sent = [json.dumps(await _search(client, q=f"album {PLANTED['discogs']}"))]
        response = await client.get(
            "/api/metadata", params={"url": store.url(f"/album/x?k={PLANTED['qobuz']}")}, headers=AUTH
        )
        assert response.status == 200
        sent.append(await response.text())
        # A refused lookup's error too.
        refused = await client.get(
            "/api/metadata", params={"url": f"http://10.0.0.1/album/{PLANTED['tidal_token']}"}, headers=AUTH
        )
        assert refused.status == 502
        sent.append(await refused.text())
        # A search is no job: it sends no event at all.
        async with client.ws_connect("/api/ws", headers=AUTH) as socket:
            await _search(client, q="album")
            with pytest.raises(asyncio.TimeoutError):
                await socket.receive(timeout=0.2)
        everything = "\n".join(sent)
        assert "[REDACTED]" in everything
        for name, secret in PLANTED.items():
            assert secret not in everything, name

    _lookup_run(test, store)
