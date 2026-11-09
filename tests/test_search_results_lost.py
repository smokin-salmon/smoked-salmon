"""The metadata search keeps what each store found, and says which Apple Music storefront or Tidal region failed.

No store is contacted: the stores are fakes, or a local server standing in for one.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from aiohttp import web

import salmon.search as salmon_search
from salmon.common import make_searchstrs
from salmon.errors import ScrapeError
from salmon.search import apple_music as search_apple
from salmon.search import metas
from salmon.search import tidal as search_tidal
from salmon.search.base import IdentData, each_failed_part_said_once, say_part_failed
from salmon.sources import AppleMusicBase
from salmon.sources.tidal import TidalBase

GETZ = ("Stan Getz", "main")
GILBERTO = ("João Gilberto", "main")
ALBUM = "Getz/Gilberto"
RELEASE = (IdentData("Stan Getz", ALBUM, 1964, 8, "WEB"), "Stan Getz - Getz/Gilberto")


def _add_store(monkeypatch: pytest.MonkeyPatch, answer: Callable[[str], Any]) -> None:
    """Add a store called Store whose search of a string gives answer(string): its releases, or raises."""

    class _Searcher:
        async def search_releases(self, searchstr: str, _limit: int) -> tuple[str, Any]:
            return "Store", answer(searchstr)

    monkeypatch.setitem(salmon_search.SEARCHSOURCES, "Store", SimpleNamespace(Searcher=_Searcher))


def _search(monkeypatch: pytest.MonkeyPatch, answers: list[Any], **kwargs: Any) -> dict[str, Any]:
    """run_metasearch over one string per answer, the store giving that answer (raising it, if an exception)."""

    def answer(searchstr: str) -> Any:
        given = answers[int(searchstr)]
        if isinstance(given, Exception):
            raise given
        return given

    _add_store(monkeypatch, answer)
    return anyio.run(
        lambda: salmon_search.run_metasearch(
            [str(i) for i in range(len(answers))], sources={"Store": None}, filter=False, **kwargs
        )
    )


def _search_getz_gilberto(monkeypatch: pytest.MonkeyPatch, artists: list[tuple[str, str]]) -> dict[str, Any]:
    """The search get_metadata runs, on a store that has the album under Stan Getz but not João Gilberto."""
    searchstrs = make_searchstrs(artists, ALBUM)
    assert len(searchstrs) == 2
    _add_store(monkeypatch, lambda searchstr: {"getz-gilberto": RELEASE} if "stan" in searchstr else {})
    return anyio.run(lambda: salmon_search.run_metasearch(searchstrs, sources={"Store": None}, filter=False))


def test_an_album_found_under_the_first_artist_only_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search_getz_gilberto(monkeypatch, [GETZ, GILBERTO])
    assert results == {"Store": {"getz-gilberto": RELEASE}}


def test_as_before_an_album_found_under_the_last_artist_only_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search_getz_gilberto(monkeypatch, [GILBERTO, GETZ])
    assert results == {"Store": {"getz-gilberto": RELEASE}}


def test_each_searchs_best_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [{"a": 1, "b": 1, "c": 1}, {"x": 2, "y": 2}, {"z": 3}])
    assert list(results["Store"]) == ["a", "x", "z", "b", "y", "c"]


def test_a_release_found_twice_keeps_the_first_searchs_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [{"b": "first", "a": "first"}, {"a": "second", "c": "second"}])
    assert results["Store"] == {"b": "first", "a": "first", "c": "second"}


def test_a_tidal_release_found_from_two_regions_is_one_release(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Searcher:
        async def search_releases(self, searchstr: str, _limit: int) -> tuple[str, Any]:
            return "Tidal", {("US", 111): "US"} if searchstr == "one" else {("DE", 111): "DE", ("DE", 222): "DE"}

    monkeypatch.setitem(salmon_search.SEARCHSOURCES, "Tidal", SimpleNamespace(Searcher=_Searcher))

    results = anyio.run(lambda: salmon_search.run_metasearch(["one", "two"], sources={"Tidal": None}, filter=False))

    assert results == {"Tidal": {("US", 111): "US", ("DE", 222): "DE"}}


def test_apples_entry_per_storefront_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [{("us", 1): "us"}, {("jp", 1): "jp"}])
    assert results["Store"] == {("us", 1): "us", ("jp", 1): "jp"}


def test_found_then_empty_stays_found(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [{"a": "first"}, {}])
    assert results == {"Store": {"a": "first"}}


def test_as_before_empty_then_found_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [{}, {"a": "second"}])
    assert results == {"Store": {"a": "second"}}


def test_as_before_empty_under_every_search_stays_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [{}, {}])
    assert results == {"Store": {}}


def test_as_before_inactive_under_every_search_stays_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [None, None])
    assert results == {"Store": None}


def test_as_before_a_search_that_errors_leaves_the_others_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [{"a": "first"}, ScrapeError("refused")])
    assert results == {"Store": {"a": "first"}}


def test_as_before_a_store_erroring_under_every_search_is_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [ScrapeError("refused"), ScrapeError("refused")])
    assert results == {}


def _with_tracks(track_count: int) -> tuple[IdentData, str]:
    return IdentData("Stan Getz", ALBUM, 1964, track_count, "WEB"), f"{track_count} tracks"


def test_the_track_count_filter_runs_on_each_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    results = _search(monkeypatch, [{"right": _with_tracks(8)}, {"wrong": _with_tracks(20)}], track_count=8)
    assert list(results["Store"]) == ["right"]


def test_the_artist_and_album_filter_runs_on_each_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"getz": {"right": RELEASE}, "gilberto": {"other": (IdentData("Other", "Other", 2001, 8, "WEB"), "")}}
    _add_store(monkeypatch, lambda searchstr: answers[searchstr])
    results = anyio.run(
        lambda: salmon_search.run_metasearch(
            ["getz", "gilberto"], sources={"Store": None}, artists=["Stan Getz"], album=ALBUM
        )
    )
    assert list(results["Store"]) == ["right"]


@asynccontextmanager
async def _serve(answer: Callable[[web.Request], Awaitable[web.Response]]) -> AsyncIterator[str]:
    """A server on localhost answering every request with answer(request): its base URL."""

    async def handler(request: web.Request) -> web.Response:
        return await answer(request)

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def _apple_album(sf: str) -> dict[str, Any]:
    return {"artistName": "Stan Getz", "collectionName": ALBUM, "collectionId": f"id-{sf}", "trackCount": 8}


def _fake_apple(monkeypatch: pytest.MonkeyPatch, storefronts: list[str], failing: list[str]) -> None:
    """Storefronts answering with one album each, but `failing`, which refuse."""

    async def get_json(_self: Any, _url: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        sf = params["country"]
        if sf in failing:
            raise ScrapeError(f"refused by {sf}")
        return {"results": [_apple_album(sf)]}

    monkeypatch.setattr(AppleMusicBase, "get_json", get_json)
    monkeypatch.setattr(search_apple, "_get_storefronts_and_langs", lambda: {sf: ["en-US"] for sf in storefronts})


def _apple_search() -> tuple[str, dict[Any, Any]]:
    return anyio.run(search_apple.Searcher().search_releases, ALBUM, 5)


def test_apple_a_failed_storefront_gets_a_line_and_the_others_are_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_apple(monkeypatch, ["us", "jp"], failing=["jp"])
    name, releases = _apple_search()
    assert (name, [key[0] for key in releases]) == ("Apple Music", ["us"])
    out = capsys.readouterr().out
    assert out.splitlines() == ["Apple Music: storefront jp failed: refused by jp"]


def test_apple_every_storefront_failing_raises_after_a_line_for_each(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_apple(monkeypatch, ["us", "jp"], failing=["us", "jp"])
    with pytest.raises(ScrapeError, match="Apple Music: every storefront failed"):
        _apple_search()
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "Apple Music: storefront us failed: refused by us",
        "Apple Music: storefront jp failed: refused by jp",
    ]


def test_apple_every_storefront_failing_reads_as_a_failed_store_in_metas(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_apple(monkeypatch, ["us"], failing=["us"])
    monkeypatch.setattr(salmon_search, "searched_sources", lambda: {"Apple Music": search_apple})
    assert metas.callback is not None
    anyio.run(metas.callback, ("getz", "gilberto"), None, 3)
    out = capsys.readouterr().out
    assert "Scrape error: Apple Music: every storefront failed." in out
    assert "Failed to scrape Apple Music." in out
    assert "No results found from Apple Music" not in out


def test_apple_a_refusal_from_the_server_prints_its_status_and_not_its_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def answer(request: web.Request) -> web.Response:
        if request.query["country"] == "jp":
            return web.Response(status=403, text="the refusal's body")
        return web.json_response({"results": [_apple_album("us")]})

    async def search() -> tuple[str, dict[Any, Any]]:
        async with _serve(answer) as base:
            monkeypatch.setattr(AppleMusicBase, "search_url", base + "/search")
            return await search_apple.Searcher().search_releases(ALBUM, 5)

    monkeypatch.setattr(search_apple, "_get_storefronts_and_langs", lambda: {"us": ["en-US"], "jp": ["ja"]})
    _, releases = anyio.run(search)
    assert [key[0] for key in releases] == ["us"]
    out = capsys.readouterr().out
    assert "Apple Music: storefront jp failed:" in out
    assert "Status code 403" in out
    assert "the refusal's body" not in out
    assert "127.0.0.1" not in out


def test_as_before_apple_with_every_storefront_answering_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_apple(monkeypatch, ["us", "jp"], failing=[])
    _, releases = _apple_search()
    assert [key[0] for key in releases] == ["us", "jp"]
    out = capsys.readouterr().out
    assert out == ""


def _fake_tidal(
    monkeypatch: pytest.MonkeyPatch,
    regions: list[str],
    failing: dict[str, Exception],
) -> None:
    """Regions answering with one album each, but those in `failing`, which raise."""

    async def search_country(_self: Any, _searchstr: str, cc: str, _limit: int) -> list[tuple[str, str, Any]]:
        if cc in failing:
            raise failing[cc]
        return [(cc, f"id-{cc}", (IdentData("Stan Getz", ALBUM, 1964, 8, "WEB"), f"[{cc}] album from {cc}"))]

    monkeypatch.setattr(search_tidal, "credentials_configured", lambda: True)
    monkeypatch.setattr(search_tidal, "COUNTRIES", regions)
    monkeypatch.setattr(search_tidal.Searcher, "_search_releases_country", search_country)


def _tidal_search() -> tuple[str, dict[Any, Any]]:
    return anyio.run(search_tidal.Searcher().search_releases, ALBUM, 5)


def test_tidal_a_failed_region_gets_a_line_and_the_others_are_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_tidal(monkeypatch, ["DE", "US"], {"DE": ScrapeError("rate limited")})
    name, releases = _tidal_search()
    assert (name, [key[0] for key in releases]) == ("Tidal", ["US"])
    out = capsys.readouterr().out
    assert out.splitlines() == ["Tidal: region DE failed: rate limited"]


def test_tidal_every_region_failing_raises_after_a_line_for_each(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_tidal(monkeypatch, ["DE", "US"], {"DE": ScrapeError("one"), "US": ScrapeError("two")})
    with pytest.raises(ScrapeError, match="Tidal: every region failed"):
        _tidal_search()
    out = capsys.readouterr().out
    assert out.splitlines() == ["Tidal: region DE failed: one", "Tidal: region US failed: two"]


def test_tidal_a_refusal_from_the_server_prints_neither_the_token_nor_the_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def answer(request: web.Request) -> web.Response:
        if request.query["countryCode"] == "DE":
            return web.Response(status=500, text="the refusal's body")
        return web.json_response({"data": []})

    async def search() -> tuple[str, dict[Any, Any]]:
        async with _serve(answer) as base:
            monkeypatch.setattr(TidalBase, "url", base)
            return await search_tidal.Searcher().search_releases(ALBUM, 5)

    async def token(_cls: Any) -> str:
        return "the-access-token"

    monkeypatch.setattr(search_tidal, "credentials_configured", lambda: True)
    monkeypatch.setattr(search_tidal, "COUNTRIES", ["DE", "US"])
    monkeypatch.setattr(TidalBase, "_ensure_token", classmethod(token))
    results = anyio.run(search)
    assert results == ("Tidal", {})
    out = capsys.readouterr().out
    assert "Tidal: region DE failed:" in out
    assert "Status code 500" in out
    assert "the-access-token" not in out
    assert "the refusal's body" not in out
    assert "127.0.0.1" not in out


def test_as_before_tidal_with_every_region_answering_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_tidal(monkeypatch, ["DE", "US"], {})
    _, releases = _tidal_search()
    assert [key[0] for key in releases] == ["DE", "US"]
    out = capsys.readouterr().out
    assert out == ""


def test_as_before_tidal_an_answer_it_cannot_read_still_fails_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tidal(monkeypatch, ["DE", "US"], {"DE": KeyError("title")})
    with pytest.raises(KeyError):
        _tidal_search()


def test_as_before_tidal_with_no_regions_finds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tidal(monkeypatch, [], {})
    results = _tidal_search()
    assert results == ("Tidal", {})


# Printed once per run of the searches, however many of them a storefront or region failed


def _searchstrs_for(count: int) -> list[str]:
    names = [GETZ, GILBERTO, ("Astrud Gilberto", "main")][:count]
    searchstrs = make_searchstrs(names, ALBUM)
    assert len(searchstrs) == count
    return searchstrs


def _run_store(store: str, searchstrs: list[str]) -> dict[str, Any]:
    return anyio.run(lambda: salmon_search.run_metasearch(searchstrs, sources={store: None}, filter=False))


@pytest.mark.parametrize("artists", [2, 3])
def test_apple_a_storefront_failing_every_search_prints_its_line_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], artists: int
) -> None:
    _fake_apple(monkeypatch, ["us", "jp"], failing=["jp"])
    results = _run_store("Apple Music", _searchstrs_for(artists))
    assert [key[0] for key in results["Apple Music"]] == ["us"]
    assert capsys.readouterr().out.splitlines() == ["Apple Music: storefront jp failed: refused by jp"]


@pytest.mark.parametrize("artists", [2, 3])
def test_tidal_a_region_failing_every_search_prints_its_line_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], artists: int
) -> None:
    _fake_tidal(monkeypatch, ["DE", "US"], {"DE": ScrapeError("rate limited")})
    results = _run_store("Tidal", _searchstrs_for(artists))
    assert [key[0] for key in results["Tidal"]] == ["US"]
    assert capsys.readouterr().out.splitlines() == ["Tidal: region DE failed: rate limited"]


def test_a_storefront_failing_two_searches_differently_prints_the_first_reason_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reasons = iter(["first reason", "second reason"])

    async def get_json(_self: Any, _url: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if params["country"] == "jp":
            raise ScrapeError(next(reasons))
        return {"results": [_apple_album("us")]}

    monkeypatch.setattr(AppleMusicBase, "get_json", get_json)
    monkeypatch.setattr(search_apple, "_get_storefronts_and_langs", lambda: {"us": ["en-US"], "jp": ["ja"]})
    _run_store("Apple Music", _searchstrs_for(2))
    assert capsys.readouterr().out.splitlines() == ["Apple Music: storefront jp failed: first reason"]


def test_two_stores_each_print_what_failed_of_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_apple(monkeypatch, ["us", "jp"], failing=["jp"])
    _fake_tidal(monkeypatch, ["DE", "US"], {"DE": ScrapeError("rate limited")})
    anyio.run(
        lambda: salmon_search.run_metasearch(
            _searchstrs_for(2), sources={"Apple Music": None, "Tidal": None}, filter=False
        )
    )
    assert sorted(capsys.readouterr().out.splitlines()) == [
        "Apple Music: storefront jp failed: refused by jp",
        "Tidal: region DE failed: rate limited",
    ]


def test_every_storefront_failing_every_search_prints_each_line_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_apple(monkeypatch, ["us", "jp"], failing=["us", "jp"])
    results = _run_store("Apple Music", _searchstrs_for(2))
    assert results == {}
    lines = capsys.readouterr().out.splitlines()
    assert lines.count("Apple Music: storefront us failed: refused by us") == 1
    assert lines.count("Apple Music: storefront jp failed: refused by jp") == 1


def test_a_later_run_prints_the_line_again(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _fake_apple(monkeypatch, ["us", "jp"], failing=["jp"])
    expected = ["Apple Music: storefront jp failed: refused by jp"]
    for _ in range(2):
        _run_store("Apple Music", _searchstrs_for(2))
        assert capsys.readouterr().out.splitlines() == expected


def test_after_the_searches_a_failed_part_is_said_at_once(capsys: pytest.CaptureFixture[str]) -> None:
    with each_failed_part_said_once():
        say_part_failed("Store: part a", "held")
        assert capsys.readouterr().out == ""
    assert capsys.readouterr().out.splitlines() == ["Store: part a failed: held"]
    say_part_failed("Store: part b", "at once")
    assert capsys.readouterr().out.splitlines() == ["Store: part b failed: at once"]
