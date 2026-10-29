"""Beatport's token: a failed login or refresh falls back to the scraper, and its requests have a timeout."""

import socket
import time

import anyio
import pytest
from aiohttp import web

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search import beatport as beatport_search
from salmon.sources import beatport
from salmon.tagger.sources import beatport as beatport_tagger

SCRAPED = ("Beatport", {"scraped": True})


def _expired_token() -> beatport.TokenStorage:
    return beatport.TokenStorage(bearer="old", expires=time.time() - 60, refresh_token="refresh", client_id="client")


async def _serve(status: int, body: dict) -> web.AppRunner:
    async def answer(_request: web.Request) -> web.Response:
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post("/v4/auth/o/token/", answer)
    app.router.add_get("/v4/docs/", answer)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _point_beatport_at(monkeypatch, port: int, stored: beatport.TokenStorage | None) -> None:
    monkeypatch.setattr(beatport.BeatportBase, "url", f"http://127.0.0.1:{port}/v4")
    monkeypatch.setattr(beatport.TokenStorage, "load", classmethod(lambda _cls: stored))
    monkeypatch.setattr(beatport.TokenStorage, "_save", lambda _self: None)


async def _search(monkeypatch) -> tuple[str, dict]:
    async def scraped(_self, _searchstr, _limit):
        return SCRAPED

    monkeypatch.setattr(beatport_search.Searcher, "search_releases_scraping", scraped)
    with anyio.fail_after(5):
        return await beatport_search.Searcher().search_releases("artist album", 5)


async def _search_with_a_refused_refresh(monkeypatch) -> tuple[str, dict]:
    runner = await _serve(401, {"error": "invalid_grant"})
    try:
        _point_beatport_at(monkeypatch, runner.addresses[0][1], _expired_token())
        return await _search(monkeypatch)
    finally:
        await runner.cleanup()


def test_a_refused_refresh_falls_back_to_the_scraper_in_search(monkeypatch) -> None:
    # The refresh ran outside handle_scrape_errors, so its ScrapeError dropped Beatport from the search
    found = anyio.run(_search_with_a_refused_refresh, monkeypatch)

    assert found == SCRAPED


def test_a_refresh_that_cannot_connect_falls_back_to_the_scraper_in_search(monkeypatch) -> None:
    # The refresh's aiohttp errors were not turned into ScrapeError, so they dropped Beatport from the search
    _point_beatport_at(monkeypatch, _closed_port(), _expired_token())

    found = anyio.run(_search, monkeypatch)

    assert found == SCRAPED


async def _search_with_a_failed_first_login(monkeypatch) -> tuple[str, dict]:
    runner = await _serve(500, {})
    try:
        _point_beatport_at(monkeypatch, runner.addresses[0][1], None)
        monkeypatch.setattr(cfg.metadata.beatport, "username", "user")
        monkeypatch.setattr(cfg.metadata.beatport, "password", "secret")
        return await _search(monkeypatch)
    finally:
        await runner.cleanup()


def test_a_failed_first_login_falls_back_to_the_scraper_in_search(monkeypatch, capsys) -> None:
    # The login ran outside handle_scrape_errors, so the docs page failing dropped Beatport from the search
    found = anyio.run(_search_with_a_failed_first_login, monkeypatch)

    out = capsys.readouterr().out
    assert found == SCRAPED
    assert "Beatport login failed: Failed to successfully scrape page. Status code: 500. Using the scraper." in out


async def _scrape_with_a_refused_refresh(monkeypatch) -> dict:
    runner = await _serve(401, {"error": "invalid_grant"})
    try:
        _point_beatport_at(monkeypatch, runner.addresses[0][1], _expired_token())

        async def scraped(_self, _url, _params):
            return {"scraped": True}

        monkeypatch.setattr(beatport_tagger.Scraper, "fetch_data_scraping", scraped)
        with anyio.fail_after(5):
            return await beatport_tagger.Scraper().fetch_data("https://www.beatport.com/release/a/1")
    finally:
        await runner.cleanup()


def test_a_refused_refresh_falls_back_to_the_scraper_when_tagging(monkeypatch) -> None:
    data = anyio.run(_scrape_with_a_refused_refresh, monkeypatch)

    assert data == {"scraped": True}


async def _refresh_from_a_slow_endpoint(monkeypatch) -> None:
    async def slow(_request: web.Request) -> web.Response:
        await anyio.sleep(2)
        return web.json_response({"access_token": "new", "expires_in": 3600, "refresh_token": "next"})

    app = web.Application()
    app.router.add_post("/v4/auth/o/token/", slow)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        _point_beatport_at(monkeypatch, runner.addresses[0][1], _expired_token())
        monkeypatch.setattr(beatport.BeatportBase, "timeout", 0.2, raising=False)
        with anyio.fail_after(5):
            await beatport.BeatportBase().refresh_bearer_token(_expired_token())
    finally:
        await runner.cleanup()


def test_a_token_refresh_that_gets_no_answer_times_out(monkeypatch) -> None:
    # It set no timeout, so aiohttp's 300 s held a search for up to five minutes when the endpoint did not answer
    with pytest.raises(ScrapeError) as excinfo:
        anyio.run(_refresh_from_a_slow_endpoint, monkeypatch)

    assert str(excinfo.value) == "BeatportBase: Request failed (TimeoutError)."
