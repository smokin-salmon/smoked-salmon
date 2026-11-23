"""A scrape that fails says which store, which release URL, what HTTP status came back and the page's title (#980).

The stores are a fake on 127.0.0.1 (helpers.ssrf.FakeStore); no real store is reached.
"""

import time
import types
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from aiohttp import web
from helpers.ssrf import LOOPBACK, FakeStore

from salmon import ssrf
from salmon.errors import ScrapeError
from salmon.sources import apple_music as apple_music_sources
from salmon.sources import beatport as beatport_sources
from salmon.sources import musicbrainz as musicbrainz_sources
from salmon.sources.base import Answer
from salmon.tagger.sources import apple_music, bandcamp, beatport, deezer, discogs, musicbrainz, qobuz, tidal
from salmon.tagger.sources.base import scrape_error_with_context


class Answering(FakeStore):
    """A store that answers every GET with `status`, `body` and `content_type`, or drops the connection."""

    def __init__(self, status: int = 200, body: str = "", content_type: str = "text/html", drop: bool = False) -> None:
        super().__init__()
        self.status, self.body, self.content_type, self.drop = status, body, content_type, drop

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request.path)
        if self.drop and request.transport is not None:
            request.transport.abort()
        return web.Response(status=self.status, text=self.body, content_type=self.content_type)


@pytest.fixture(autouse=True)
def loopback(monkeypatch) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)


def _scrape(store: Answering, path: str = "/album/x", suffix: str = "") -> tuple[str, str]:
    """The URL of `path` on the store, and the message of the ScrapeError Bandcamp's scrape of path+suffix raised."""
    url = ""

    async def run() -> str:
        nonlocal url
        async with store.serving():
            url = store.url(path)
            with pytest.raises(ScrapeError) as excinfo:
                await bandcamp.Scraper().scrape_release(url + suffix)
            return str(excinfo.value)

    message = anyio.run(run)
    return url, message


def test_an_html_page_with_no_release_names_the_store_the_url_the_status_and_the_title() -> None:
    store = Answering(body="<html><head><title>Something else</title></head><body>hi</body></html>")

    url, message = _scrape(store)

    assert message.startswith(
        f'Bandcamp: could not read the release at {url} (HTTP 200, page titled "Something else"): '
    )
    assert not message.endswith(": ")


def test_a_404_names_the_store_the_url_and_the_status() -> None:
    store = Answering(status=404, body="<html><head><title>Not Found</title></head></html>")

    url, message = _scrape(store)

    assert message == f'Bandcamp: could not read the release at {url} (HTTP 404, page titled "Not Found").'


def test_a_page_with_no_title_leaves_the_title_out() -> None:
    url, message = _scrape(Answering(status=404, body="<html><body>gone</body></html>"))

    assert message == f"Bandcamp: could not read the release at {url} (HTTP 404)."


def test_a_long_title_is_trimmed_and_capped() -> None:
    _, message = _scrape(Answering(status=404, body=f"<title>\n  {'ab ' * 60}\n</title>"))

    title = message.split('page titled "')[1].split('")')[0]
    assert 0 < len(title) <= 80
    assert title == title.strip()
    assert "\n" not in title


def test_a_title_with_control_characters_is_quoted_without_them() -> None:
    _, message = _scrape(Answering(status=404, body="<title>a\x1b[2Jb\x07c</title>"))

    assert 'page titled "a[2Jbc"' in message
    assert "\x1b" not in message


def test_a_json_store_answering_something_else_names_the_status_and_no_title(monkeypatch) -> None:
    store = Answering(body="not json at all", content_type="text/plain")

    async def run() -> str:
        async with store.serving():
            monkeypatch.setattr(deezer.Scraper, "url", store.url(""))
            with pytest.raises(ScrapeError) as excinfo:
                await deezer.Scraper().scrape_release("https://www.deezer.com/album/1", rls_id="1")
            return str(excinfo.value)

    message = anyio.run(run)

    assert message.startswith("Deezer: could not read the release at https://www.deezer.com/album/1 (HTTP 200): ")


def test_a_json_store_answering_200_with_a_check_page_names_its_title(monkeypatch) -> None:
    store = Answering(body="<title>Client Challenge</title>", content_type="text/html")

    async def run() -> str:
        async with store.serving():
            monkeypatch.setattr(deezer.Scraper, "url", store.url(""))
            with pytest.raises(ScrapeError) as excinfo:
                await deezer.Scraper().scrape_release("https://www.deezer.com/album/1", rls_id="1")
            return str(excinfo.value)

    message = anyio.run(run)

    assert '(HTTP 200, page titled "Client Challenge")' in message


def test_deezers_internal_page_answering_200_with_a_check_page_names_its_title(monkeypatch) -> None:
    store = Answering(body="<title>Client Challenge</title>")

    async def run() -> str:
        async with store.serving():
            monkeypatch.setattr(deezer.Scraper, "url", store.url(""))
            monkeypatch.setattr(deezer.Scraper, "site_url", store.url(""))

            async def get_json(self: Any, *_args: Any, **_kwargs: Any) -> dict:
                return {}

            monkeypatch.setattr(deezer.Scraper, "get_json", get_json)
            with pytest.raises(ScrapeError) as excinfo:
                await deezer.Scraper().scrape_release("https://www.deezer.com/album/1", rls_id="1")
            return str(excinfo.value)

    message = anyio.run(run)

    assert '(HTTP 200, page titled "Client Challenge")' in message


def test_the_query_and_the_fragment_appear_nowhere() -> None:
    url, message = _scrape(Answering(status=404), suffix="?token=secret#frag")

    assert "secret" not in message
    assert "frag" not in message
    assert url in message


def test_a_request_that_fails_before_any_answer_has_no_parenthesis() -> None:
    url, message = _scrape(Answering(drop=True))

    assert message.startswith(f"Bandcamp: could not read the release at {url}: ")


def test_the_reason_does_not_name_the_store_twice() -> None:
    _, message = _scrape(Answering(status=404))

    assert message.count("Bandcamp") == 1


@pytest.mark.parametrize("error", [KeyError("x"), AttributeError("y")], ids=["key", "attribute"])
def test_other_errors_keep_their_way(error: Exception) -> None:
    store = Answering(body="<title>t</title>")

    class Scraper(bandcamp.Scraper):
        async def parse_tracks(self, soup: Any) -> Any:
            raise error

    async def run() -> None:
        async with store.serving():
            await Scraper().scrape_release(store.url("/album/x"))

    with pytest.raises(type(error)):
        anyio.run(run)


def test_a_scraper_with_no_store_name_is_named_by_its_class() -> None:
    class Nameless:
        store = None
        last_answer = None

    error = scrape_error_with_context(Nameless(), "https://u:p@x.test/a/b?q=1#f", ScrapeError("boom"))

    assert str(error) == "Nameless: could not read the release at https://x.test/a/b: boom"


def test_a_reason_that_is_only_the_status_line_is_not_said_twice() -> None:
    answer = Answer(404)
    scraper = types.SimpleNamespace(store="Discogs", last_answer=answer)

    error = scrape_error_with_context(scraper, "https://www.discogs.com/release/1", ScrapeError("Discogs: HTTP 404."))

    assert str(error) == "Discogs: could not read the release at https://www.discogs.com/release/1 (HTTP 404)."


def test_a_status_line_of_another_status_is_kept() -> None:
    scraper = types.SimpleNamespace(store="Discogs", last_answer=Answer(200))

    error = scrape_error_with_context(scraper, "https://www.discogs.com/release/1", ScrapeError("Discogs: HTTP 404."))

    assert str(error).endswith("(HTTP 200): HTTP 404.")


def _tidal_scraper_after_an_answer() -> tidal.Scraper:
    scraper = tidal.Scraper()
    scraper.record_answer(401)
    return scraper


def test_a_failed_tidal_token_refresh_shows_no_status(monkeypatch) -> None:
    async def refused(cls: Any) -> str:
        raise ScrapeError("Tidal authentication failed (HTTP 500).")

    monkeypatch.setattr(tidal.Scraper, "_ensure_token", classmethod(refused))
    scraper = _tidal_scraper_after_an_answer()

    async def run() -> None:
        await scraper.scrape_release("https://listen.tidal.com/album/1", rls_id="1")

    with pytest.raises(ScrapeError) as excinfo:
        anyio.run(run)

    assert str(excinfo.value).startswith("Tidal: could not read the release at https://listen.tidal.com/album/1: ")


def test_a_failed_apple_music_token_bootstrap_shows_no_status(monkeypatch) -> None:
    async def refused(cls: Any) -> str:
        raise ScrapeError("Failed to load Apple Music homepage")

    monkeypatch.setattr(apple_music.Scraper, "_get_token", classmethod(refused))
    scraper = apple_music.Scraper()
    scraper.record_answer(200)
    url = "https://music.apple.com/us/album/x/1"

    async def run() -> None:
        await scraper.scrape_release(url, rls_id=("us", "en-US", "1"))

    with pytest.raises(ScrapeError) as excinfo:
        anyio.run(run)

    assert str(excinfo.value).startswith(f"Apple Music: could not read the release at {url}: ")


class FirstAnswered(FakeStore):
    """A store that answers its first request, any path, with `first`, then drops each one; with no `first`, all."""

    def __init__(self, first: tuple[int, str, dict[str, str]] | None) -> None:
        super().__init__()
        self.first = first

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request.path)
        if len(self.requests) == 1 and self.first:
            status, body, headers = self.first
            return web.Response(status=status, text=body, headers=headers)
        if request.transport is not None:
            request.transport.abort()
        return web.Response()


JSON = {"Content-Type": "application/json"}
Pointed = tuple[Any, str, Any]  # The scraper, the release URL and the rls_id to scrape it with


def _point_deezer(monkeypatch: pytest.MonkeyPatch, base: str) -> Pointed:
    monkeypatch.setattr(deezer.Scraper, "url", base + "/api")
    monkeypatch.setattr(deezer.Scraper, "site_url", base)
    return deezer.Scraper(), "https://www.deezer.com/album/1", None


def _point_beatport(monkeypatch: pytest.MonkeyPatch, base: str) -> Pointed:
    expired = beatport_sources.TokenStorage(bearer="b", expires=time.time() - 60, refresh_token="r", client_id="c")
    monkeypatch.setattr(beatport.Scraper, "url", base + "/v4")
    monkeypatch.setattr(beatport_sources.TokenStorage, "load", classmethod(lambda _cls: expired))
    monkeypatch.setattr(beatport_sources.TokenStorage, "_save", lambda _self: None)
    return beatport.Scraper(), base + "/release/x/1", None


def _point_musicbrainz(monkeypatch: pytest.MonkeyPatch, base: str) -> Pointed:
    monkeypatch.setattr(musicbrainz.Scraper, "api_url", base)
    monkeypatch.setattr(musicbrainz_sources, "RETRY_WAIT", 0)
    return musicbrainz.Scraper(), "https://musicbrainz.org/release/0a1b2c3d-0000-4000-8000-000000000001", None


def _point_tidal(monkeypatch: pytest.MonkeyPatch, base: str) -> Pointed:
    async def token(_cls: Any) -> str:
        return "t"

    monkeypatch.setattr(tidal.Scraper, "_ensure_token", classmethod(token))
    monkeypatch.setattr(tidal.Scraper, "url", base)
    return tidal.Scraper(), "https://listen.tidal.com/album/1", ("US", "1")


def _point_apple_music(monkeypatch: pytest.MonkeyPatch, base: str) -> Pointed:
    async def token(_cls: Any) -> str:
        return "t"

    monkeypatch.setattr(apple_music.Scraper, "_get_token", classmethod(token))
    monkeypatch.setattr(apple_music_sources, "AMP_API_URL", base)
    return apple_music.Scraper(), "https://music.apple.com/us/album/x/1", ("us", "en-US", "1")


def _point_bandcamp(_monkeypatch: pytest.MonkeyPatch, base: str) -> Pointed:
    return bandcamp.Scraper(), base + "/album/x", None


def _point_discogs(monkeypatch: pytest.MonkeyPatch, base: str) -> Pointed:
    monkeypatch.setattr(discogs.Scraper, "url", base)
    return discogs.Scraper(), "https://www.discogs.com/release/1", None


def _point_qobuz(monkeypatch: pytest.MonkeyPatch, base: str) -> Pointed:
    monkeypatch.setattr(qobuz.Scraper, "configured", staticmethod(lambda: False))
    monkeypatch.setattr(qobuz, "PUBLIC_PAGE", base + "/album/{album_id}")
    return qobuz.Scraper(), "https://www.qobuz.com/gb-en/album/x/abc1", None


TIDAL_FIRST_PAGE = '{"data": {"relationships": {"items": {"links": {"meta": {"nextCursor": "c"}}}}}}'
APPLE_FIRST_PAGE = '{"data": [{"relationships": {"tracks": {"next": "/n"}}}]}'

# Each store pointed at the fake, and the answer its first request gets so that it sends a second; None for a store
# whose scrape sends one request: an answer is recorded before it instead, as an earlier request's would be.
STORES: dict[str, tuple[Callable[[pytest.MonkeyPatch, str], Pointed], tuple[int, str, dict[str, str]] | None]] = {
    "Deezer": (_point_deezer, (200, "{}", JSON)),
    "Beatport": (_point_beatport, (200, "{}", JSON)),
    "MusicBrainz": (_point_musicbrainz, (503, "", {"Retry-After": "0"})),
    "Tidal": (_point_tidal, (200, TIDAL_FIRST_PAGE, JSON)),
    "Apple Music": (_point_apple_music, (200, APPLE_FIRST_PAGE, JSON)),
    "Bandcamp": (_point_bandcamp, None),
    "Discogs": (_point_discogs, None),
    "Qobuz": (_point_qobuz, None),
}


def _scrape_answered_then_dropped(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[str, list[str]]:
    """The message of the ScrapeError `name`'s scrape raised against FirstAnswered, and the paths it asked for."""
    point, first = STORES[name]
    store = FirstAnswered(first)

    async def run() -> str:
        async with store.serving():
            scraper, url, rls_id = point(monkeypatch, store.url(""))
            if first is None:
                scraper.record_answer(200)
            with anyio.fail_after(10), pytest.raises(ScrapeError) as excinfo:
                await scraper.scrape_release(url, rls_id=rls_id)
            return str(excinfo.value)

    return anyio.run(run), store.requests


@pytest.mark.parametrize("name", list(STORES))
def test_a_request_dropped_after_an_answer_never_shows_that_answers_status(monkeypatch, name: str) -> None:
    # Deezer: its API answers, its internal page drops. Beatport: the token refresh answers, the scraper's page drops.
    # MusicBrainz: a 503, then its retry drops. Apple Music, Tidal: the album answers, its next track page drops.
    message, requests = _scrape_answered_then_dropped(monkeypatch, name)

    assert len(requests) >= (2 if STORES[name][1] else 1)
    assert message.startswith(f"{name}: could not read the release at ")
    assert "(HTTP" not in message
