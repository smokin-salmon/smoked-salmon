"""get_json promises ScrapeError, so a network failure must not escape as an aiohttp error."""

import anyio
import pytest
from aiohttp.client_exceptions import ClientConnectorDNSError
from aiohttp.client_reqrep import ConnectionKey

from salmon.errors import ScrapeError
from salmon.sources import base


class _FailingSession:
    def __init__(self, error: Exception):
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, *_args, **_kwargs):
        raise self._error


def _get_json(monkeypatch, error: Exception):
    monkeypatch.setattr(base.aiohttp, "ClientSession", lambda *_args, **_kwargs: _FailingSession(error))
    scraper = base.BaseScraper()
    scraper.url = "https://api.example.test"
    return anyio.run(scraper.get_json, "/release/1")


def _dns_error() -> ClientConnectorDNSError:
    key = ConnectionKey("api.example.test", 443, True, True, None, None, None)
    return ClientConnectorDNSError(key, OSError(1, "Name or service not known"))


def test_a_connection_error_becomes_a_scrape_error(monkeypatch) -> None:
    # A DNS/connection failure raises this; get_json's contract says ScrapeError.
    with pytest.raises(ScrapeError):
        _get_json(monkeypatch, _dns_error())


def test_a_timeout_becomes_a_scrape_error(monkeypatch) -> None:
    with pytest.raises(ScrapeError):
        _get_json(monkeypatch, TimeoutError())


def test_the_message_does_not_repeat_the_request_url(monkeypatch) -> None:
    # aiohttp embeds the full URL, query params included, in its error text.
    with pytest.raises(ScrapeError) as excinfo:
        _get_json(monkeypatch, _dns_error())
    assert "api.example.test" not in str(excinfo.value)
