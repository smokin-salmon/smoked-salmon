"""Bandcamp's "Client Challenge" page (#985): asked again once as a browser, and named when it stays.

From some networks Bandcamp answers a page with HTTP 200 and a JavaScript check titled "Client Challenge", which the
parser failed on with "Failed to parse scraped title.". The first fetch goes through a fake aiohttp session, and the
retry through a fake curl_cffi module: no test reaches the network.
"""

import sys
import types
from typing import Any

import anyio
import pytest
from aiohttp import web
from yarl import URL

from salmon import cfg
from salmon.config.validations import ProxyCfg, ProxyServicesCfg
from salmon.errors import ScrapeError
from salmon.search.bandcamp import Searcher
from salmon.sources import bandcamp_challenge, base
from salmon.tagger.sources.bandcamp import Scraper

ALBUM_URL = "https://adventurousmusic.bandcamp.com/album/variations"
CHALLENGE = b"<html><head><title>\n  Client Challenge\n</title></head><body>enable JavaScript</body></html>"
ALBUM = b"<html><head><title>Variations | Adventurous Music</title></head><body>the album</body></html>"
SEARCH = b"""<html><head><title>Search: variations | Bandcamp</title></head><body><ul class="result-items">
<li class="searchresult data-search"><div class="result-info">
  <div class="heading"><a>Variations</a></div>
  <div class="subhead">by Adventurous Music</div>
  <div class="length">8 tracks</div>
  <div class="released">released March 1, 2024</div>
  <div class="itemurl"><a>https://adventurousmusic.bandcamp.com/album/variations</a></div>
</div></li></ul></body></html>"""
STILL = (
    'Bandcamp sent a check page ("Client Challenge") instead of the release, also when asked again as a browser. '
    "Use another source, or try again later."
)
CUSTOM_DOMAIN = (
    'Bandcamp sent a check page ("Client Challenge") instead of the release. Use another source, or try again later.'
)


class _Response:
    def __init__(self, body: bytes, url: URL):
        self.status = 200
        self._body = body
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def read(self) -> bytes:
        return self._body


class _Session:
    """aiohttp.ClientSession's part fetch_page uses, answering every GET with `body`.

    A URL in `redirects` (with its query) is redirected to the one it maps to, when the GET follows redirects.
    """

    def __init__(self, body: bytes, calls: list[str], redirects: dict[str, str], connector: Any = None):
        self._body = body
        self._calls = calls
        self._redirects = redirects
        self._connector = connector

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        if self._connector is not None:
            await self._connector.close()

    def get(self, url: str, params: dict | None = None, allow_redirects: bool = True, **_kwargs: Any) -> _Response:
        self._calls.append(url)
        asked = str(URL(url).update_query(params or {}))
        final = self._redirects.get(asked, asked) if allow_redirects else asked
        return _Response(self._body, URL(final))


class _CurlError(Exception):
    pass


class _Timeout(_CurlError):
    pass


class _CurlResponse:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content


def _fake_curl_cffi(answer: tuple[int, bytes] | Exception, calls: list[dict[str, Any]]) -> types.ModuleType:
    """A curl_cffi module whose AsyncSession records each GET and answers it with `answer`."""

    class AsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str, **kwargs: Any) -> _CurlResponse:
            calls.append({"url": url, **kwargs})
            if isinstance(answer, Exception):
                raise answer
            return _CurlResponse(*answer)

    module = types.ModuleType("curl_cffi")
    module.AsyncSession = AsyncSession  # type: ignore[attr-defined]
    module.CurlError = _CurlError  # type: ignore[attr-defined]
    return module


@pytest.fixture
def first_fetches(monkeypatch: pytest.MonkeyPatch):
    """Answer the first fetch with a given body, after the given redirects; returns the setter and the URLs fetched."""
    calls: list[str] = []

    def answer(body: bytes, redirects: dict[str, str] | None = None) -> None:
        monkeypatch.setattr(
            base.aiohttp,
            "ClientSession",
            lambda *_args, connector=None, **_kwargs: _Session(body, calls, redirects or {}, connector),
        )

    return answer, calls


@pytest.fixture
def retries(monkeypatch: pytest.MonkeyPatch):
    """Answer the browser retry with a given status and body, or an error; returns the setter and the GETs made."""
    calls: list[dict[str, Any]] = []

    def answer(response: tuple[int, bytes] | Exception) -> None:
        monkeypatch.setitem(sys.modules, "curl_cffi", _fake_curl_cffi(response, calls))

    return answer, calls


def _fetch(url: str = ALBUM_URL, params: dict | None = None):
    return anyio.run(Scraper().fetch_page, url, params)


def _title(soup) -> str:
    return soup.title.get_text().strip()


def test_an_album_page_is_returned_without_a_retry(first_fetches, retries) -> None:
    first_fetches[0](ALBUM)
    retries[0](AssertionError("no retry"))

    assert _title(_fetch()) == "Variations | Adventurous Music"
    assert retries[1] == []


def test_a_check_page_is_asked_for_once_more_as_a_browser(first_fetches, retries) -> None:
    first_fetches[0](CHALLENGE)
    retries[0]((200, ALBUM))

    soup = _fetch(params={"a": "b"})

    assert _title(soup) == "Variations | Adventurous Music"
    assert first_fetches[1] == [ALBUM_URL]
    assert retries[1] == [
        {
            # The URL the check page came from carries the query: it is not passed again.
            "url": f"{ALBUM_URL}?a=b",
            "params": None,
            "proxies": {"all": ""},
            "timeout": 7,
            "allow_redirects": False,
            "impersonate": "firefox",
        }
    ]


def test_a_check_page_that_stays_is_named_instead_of_failing_to_parse(first_fetches, retries) -> None:
    # The reporter's case: "ScrapeError: Failed to parse scraped title."
    first_fetches[0](CHALLENGE)
    retries[0]((200, CHALLENGE))

    with pytest.raises(ScrapeError) as excinfo:
        anyio.run(Scraper().scrape_release, ALBUM_URL)

    assert str(excinfo.value) == STILL
    assert len(retries[1]) == 1


@pytest.mark.parametrize("status", [301, 302, 403, 503])
def test_a_retry_answered_with_anything_but_the_page_is_a_check_page_that_stays(first_fetches, retries, status) -> None:
    first_fetches[0](CHALLENGE)
    retries[0]((status, ALBUM))

    with pytest.raises(ScrapeError, match=r"also when asked again as a browser"):
        _fetch()
    assert len(retries[1]) == 1


def test_a_check_page_from_a_custom_domain_is_not_asked_for_again(first_fetches, retries) -> None:
    # The retry skips salmon web's public-address guard, which only Bandcamp's own hosts can do without.
    first_fetches[0](CHALLENGE)
    retries[0]((200, ALBUM))

    with pytest.raises(ScrapeError) as excinfo:
        _fetch("https://music.example.org/album/x")

    assert str(excinfo.value) == CUSTOM_DOMAIN
    assert retries[1] == []


@pytest.mark.parametrize("host", ["bandcamp.com.example.org", "notbandcamp.com"])
def test_a_host_that_only_looks_like_bandcamps_is_not_asked_for_again(first_fetches, retries, host) -> None:
    first_fetches[0](CHALLENGE)
    retries[0]((200, ALBUM))

    with pytest.raises(ScrapeError, match=r"instead of the release\. Use"):
        _fetch(f"https://{host}/album/x")
    assert retries[1] == []


@pytest.mark.parametrize(
    ("configured", "sent"),
    [
        ("socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1080"),
        ("socks4://user:pw@127.0.0.1:1080", "socks4a://user:pw@127.0.0.1:1080"),
        ("http://127.0.0.1:3128", "http://127.0.0.1:3128"),
    ],
)
def test_the_retry_goes_through_bandcamps_proxy_which_resolves_names(
    monkeypatch: pytest.MonkeyPatch, first_fetches, retries, configured: str, sent: str
) -> None:
    monkeypatch.setattr(cfg, "proxy", ProxyCfg(services=ProxyServicesCfg(bandcamp=configured)))
    first_fetches[0](CHALLENGE)
    retries[0]((200, ALBUM))

    _fetch()

    assert [call["proxies"] for call in retries[1]] == [{"all": sent}]


def test_the_search_is_asked_for_once_more_as_a_browser(first_fetches, retries) -> None:
    # The search does not follow redirects: the retry asks for the URL it asked for, its query included.
    first_fetches[0](CHALLENGE, {"https://bandcamp.com/search/?q=variations": "https://elsewhere.bandcamp.com/"})
    retries[0]((200, SEARCH))

    _, releases = anyio.run(Searcher().search_releases, "variations", 5)

    assert list(releases) == [("adventurousmusic.bandcamp.com", "album", "variations")]
    retried = [(call["url"], call["params"]) for call in retries[1]]
    assert retried == [("https://bandcamp.com/search/?q=variations", None)]


@pytest.mark.parametrize(
    "asked",
    ["http://adventurousmusic.bandcamp.com/album/variations", "https://adventurousmusic.bandcamp.com/album/old-slug"],
)
def test_the_retry_asks_for_the_page_a_redirect_led_to(first_fetches, retries, asked: str) -> None:
    # Asked for again with redirects off, the URL first asked for would only answer with the redirect.
    first_fetches[0](CHALLENGE, {asked: ALBUM_URL})
    retries[0]((200, ALBUM))

    assert _title(_fetch(asked)) == "Variations | Adventurous Music"
    assert [(call["url"], call["params"]) for call in retries[1]] == [(ALBUM_URL, None)]


def test_params_a_redirect_dropped_are_passed_again(first_fetches, retries) -> None:
    old = "https://adventurousmusic.bandcamp.com/album/old-slug"
    first_fetches[0](CHALLENGE, {f"{old}?a=b&c=d": f"{ALBUM_URL}?a=b"})
    retries[0]((200, ALBUM))

    _fetch(old, params={"a": "b", "c": "d"})

    assert [(call["url"], call["params"]) for call in retries[1]] == [(f"{ALBUM_URL}?a=b", {"c": "d"})]


def test_a_redirect_to_a_custom_domain_is_not_asked_for_again(first_fetches, retries) -> None:
    first_fetches[0](CHALLENGE, {ALBUM_URL: "https://music.example.org/album/variations"})
    retries[0]((200, ALBUM))

    with pytest.raises(ScrapeError) as excinfo:
        _fetch()

    assert str(excinfo.value) == CUSTOM_DOMAIN
    assert retries[1] == []


def test_the_search_names_a_check_page_that_stays(first_fetches, retries) -> None:
    first_fetches[0](CHALLENGE)
    retries[0]((200, CHALLENGE))

    with pytest.raises(ScrapeError) as excinfo:
        anyio.run(Searcher().search_releases, "variations", 5)

    assert STILL in str(excinfo.value)
    assert len(retries[1]) == 1


def test_curl_cffi_failing_to_load_is_a_scrape_error(monkeypatch: pytest.MonkeyPatch, first_fetches) -> None:
    first_fetches[0](CHALLENGE)
    monkeypatch.setitem(sys.modules, "curl_cffi", None)

    with pytest.raises(ScrapeError) as excinfo:
        _fetch()

    assert "Bandcamp" in str(excinfo.value)
    assert "curl_cffi" in str(excinfo.value)


def test_a_failed_retry_names_the_error_but_not_the_url(first_fetches, retries) -> None:
    first_fetches[0](CHALLENGE)
    retries[0](_Timeout(f"Failed to perform, {ALBUM_URL}?secret=1"))

    with pytest.raises(ScrapeError) as excinfo:
        _fetch(params={"secret": "1"})

    assert str(excinfo.value) == "Bandcamp: Request failed when asked again as a browser (_Timeout)."
    assert "secret" not in str(excinfo.value)


async def _get_from_a_local_server(path: str) -> tuple[int, bytes]:
    async def handler(request: web.Request) -> web.Response:
        if request.path == "/moved":
            raise web.HTTPFound("/album")
        return web.Response(body=ALBUM, content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        port = runner.addresses[0][1]
        return await bandcamp_challenge._impersonated_get(f"http://127.0.0.1:{port}{path}", None, None, 7)
    finally:
        await runner.cleanup()


def test_the_real_curl_cffi_returns_the_status_and_body() -> None:
    assert anyio.run(_get_from_a_local_server, "/album") == (200, ALBUM)


def test_the_real_curl_cffi_does_not_follow_a_redirect() -> None:
    status, _ = anyio.run(_get_from_a_local_server, "/moved")
    assert status == 302


@pytest.mark.parametrize("variable", ["http_proxy", "ALL_PROXY"])
def test_without_a_proxy_set_the_retry_ignores_the_environments(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    # As aiohttp does for every other request: [proxy] alone chooses. Nothing listens on port 9.
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "http://127.0.0.1:9")

    assert anyio.run(_get_from_a_local_server, "/album") == (200, ALBUM)


async def _fetch_page_at_a_local_server(path: str, params: dict) -> tuple[str, str]:
    async def handler(request: web.Request) -> web.Response:
        if request.path == "/old-slug":
            raise web.HTTPMovedPermanently("/album?a=b")
        return web.Response(body=ALBUM, content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        port = runner.addresses[0][1]
        _, final = await base.BaseScraper()._fetch_page_at(f"http://127.0.0.1:{port}{path}", params)
        return final.path, final.query_string
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    ("path", "params", "final"),
    [("/album", {"a": "b"}, ("/album", "a=b")), ("/old-slug", {"a": "b", "c": "d"}, ("/album", "a=b"))],
)
def test_aiohttp_gives_the_url_a_redirect_led_to_with_its_query(path: str, params: dict, final: tuple) -> None:
    # What the fake session above copies: the retry's URL and params rest on it.
    assert anyio.run(_fetch_page_at_a_local_server, path, params) == final
