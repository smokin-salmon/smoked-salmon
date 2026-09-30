import anyio
import pytest

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search.qobuz import Searcher
from salmon.sources import qobuz as qobuz_base
from salmon.tagger.sources.qobuz import Scraper

QOBUZ_URL = "https://www.qobuz.com/au-en/album/journaling-illy/ul39e7xjbuqrb"


def _no_network(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise AssertionError("no request may be made while Qobuz is unconfigured")

    monkeypatch.setattr(qobuz_base.QobuzBase, "get_json", fail)


def test_search_reports_inactive_without_an_app_id(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", None)
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "token-456")
    _no_network(monkeypatch)

    result = anyio.run(Searcher().search_releases, "illy journaling", 5)

    assert result == ("Qobuz", None)


def test_search_reports_inactive_without_a_user_token(monkeypatch) -> None:
    # Qobuz answers "User authentication is required" to an app id on its own.
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", "app-123")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", None)
    _no_network(monkeypatch)

    result = anyio.run(Searcher().search_releases, "illy journaling", 5)

    assert result == ("Qobuz", None)


def test_api_client_refuses_a_qobuz_url_without_an_app_id(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", None)
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "token-456")
    _no_network(monkeypatch)

    with pytest.raises(ScrapeError, match="inactive"):
        anyio.run(qobuz_base.QobuzBase().fetch_data, QOBUZ_URL)


def test_headers_never_carry_a_none_value(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", "app-123")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", None)

    assert Scraper().headers == {"X-App-Id": "app-123"}


def test_shipped_placeholder_credentials_are_not_configured(monkeypatch) -> None:
    # config.default.toml ships these two literal strings; a user who never set up Qobuz still has
    # them, so they must not count as configured.
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", "app-id")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "user_auth_token")

    assert qobuz_base.QobuzBase.configured() is False
    assert Scraper().headers == {}


def test_search_reports_inactive_with_the_shipped_default_placeholders(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", "app-id")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "user_auth_token")
    _no_network(monkeypatch)

    result = anyio.run(Searcher().search_releases, "illy journaling", 5)

    assert result == ("Qobuz", None)


def test_real_looking_credentials_are_configured(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", "a1b2c3")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "d4e5f6")

    assert qobuz_base.QobuzBase.configured() is True
    assert Scraper().headers == {"X-App-Id": "a1b2c3", "X-User-Auth-Token": "d4e5f6"}


def test_configured_search_sends_both_headers(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata.qobuz, "app_id", "app-123")
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "token-456")
    seen: list[dict] = []

    async def fake_get_json(_self, _url, params=None, headers=None):
        seen.append(dict(headers or {}))
        return {"albums": {"items": []}}

    monkeypatch.setattr(qobuz_base.QobuzBase, "get_json", fake_get_json)

    result = anyio.run(Searcher().search_releases, "illy journaling", 5)

    assert result == ("Qobuz", {})
    assert seen == [{"X-App-Id": "app-123", "X-User-Auth-Token": "token-456"}]
