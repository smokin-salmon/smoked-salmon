"""Libble authenticate must not mark the client authenticated before its scrape (#591)."""

import asyncio

import anyio
import pytest

from salmon.errors import LoginError
from salmon.trackers.base import HttpResponse
from salmon.trackers.libble import LibbleApi

_PASSKEY = "a" * 32
_OK_PAGE = f'userinfo var authkey = "auth123" passkey={_PASSKEY}'


def _api() -> LibbleApi:
    api = LibbleApi()
    api.base_url = "https://libble.me"
    return api


def test_release_types_map_types_missing_on_libble() -> None:
    api = _api()

    assert api.release_types["Anthology"] == api.release_types["Compilation"]
    assert api.release_types["DJ Mix"] == api.release_types["Mixtape"]
    assert api.release_types["Concert Recording"] == api.release_types["Live album"]
    assert api.release_types["Demo"] == api.release_types["Unknown"]
    assert api.release_types["Split"] == api.release_types["Unknown"]


def test_authenticate_scrape_bypasses_ensure_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api()
    seen: dict = {}

    async def fake_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen["needs_authkey"] = kwargs.get("needs_authkey", True)
        seen["authenticated_during"] = api._authenticated
        return HttpResponse(text=_OK_PAGE, url=api.base_url + "/upload.php", status=200)

    monkeypatch.setattr(api, "_request", fake_request)
    anyio.run(api.authenticate)

    assert seen["needs_authkey"] is False
    assert seen["authenticated_during"] is False
    assert api._authenticated is True
    assert api.authkey == "auth123"
    assert api.passkey == _PASSKEY


def test_cancelled_authenticate_leaves_client_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api()

    async def fake_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError

    monkeypatch.setattr(api, "_request", fake_request)
    with pytest.raises(asyncio.CancelledError):
        anyio.run(api.authenticate)

    assert api._authenticated is False
    assert api.authkey is None
    assert api.passkey is None


def test_rejected_scrape_leaves_client_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api()

    async def fake_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        return HttpResponse(text="login please", url=api.base_url + "/login.php", status=200)

    monkeypatch.setattr(api, "_request", fake_request)
    with pytest.raises(LoginError):
        anyio.run(api.authenticate)

    assert api._authenticated is False
