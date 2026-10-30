"""fetch_page goes through BaseScraper._session, so its errors are mapped as get_json's are."""

import anyio
import pytest
from aiohttp import web

from salmon.errors import ScrapeError
from salmon.search import SEARCHSOURCES
from salmon.sources import base
from salmon.tagger.sources import METASOURCES


async def _serve(handler) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


def _url(runner: web.AppRunner) -> str:
    return f"http://127.0.0.1:{runner.addresses[0][1]}"


async def _fetch_a_redirect_loop() -> str:
    async def loop(request: web.Request) -> web.Response:
        raise web.HTTPFound(f"/loop?q={request.query.get('q', '')}")

    runner = await _serve(loop)
    try:
        with pytest.raises(ScrapeError) as excinfo:
            await base.BaseScraper().fetch_page(_url(runner) + "/loop", params={"q": "private-search-words"})
        return str(excinfo.value)
    finally:
        await runner.cleanup()


def test_fetch_page_does_not_repeat_the_url_of_a_failed_request() -> None:
    # "Failed to scrape page: {e}" printed aiohttp's TooManyRedirects text, which holds the URL and its query
    message = anyio.run(_fetch_a_redirect_loop)

    assert "private-search-words" not in message
    assert "TooManyRedirects" in message


def test_every_store_is_named_in_messages_as_the_search_and_tagger_name_it() -> None:
    # "Searcher: Request failed (TimeoutError)." did not say which store of a multi-store search failed
    for name, module in SEARCHSOURCES.items():
        assert module.Searcher.store == name
    for name, module in METASOURCES.items():
        assert module.Scraper.store == name
