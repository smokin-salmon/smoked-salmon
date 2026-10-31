"""Picking a search row that lacks the fields print_torrents reads (#822).

print_search_results skips such a row, but its number still picks it. _confirm_group_id must fetch the
group then, as it does for a typed group ID, instead of printing the row and stopping on a KeyError.
A complete row is still printed as it is, with no request.
"""

import sys
from pathlib import Path

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import dupe_checker
from salmon.uploader.dupe_checker import _confirm_group_id

GROUP = {
    "group": {"id": 5, "name": "Fetched Album", "year": 2001, "musicInfo": {"artists": [{"name": "Fetched Artist"}]}},
    "torrents": [{"id": 50, "media": "CD", "format": "FLAC", "encoding": "Lossless"}],
}


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.api_key = ""
        super().__init__()
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


@pytest.fixture(autouse=True)
def answer_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def yes(*args, **kwargs) -> str:
        return "y"

    monkeypatch.setattr(dupe_checker.interaction, "prompt", yes)


async def _confirm(row: dict) -> tuple[bool, list[str]]:
    hits: list[str] = []

    async def ajax(request: web.Request) -> web.Response:
        hits.append(request.query["action"])
        return web.json_response({"status": "success", "response": GROUP})

    app = web.Application()
    app.router.add_get("/ajax.php", ajax)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    api = FakeApi(f"http://127.0.0.1:{runner.addresses[0][1]}")
    try:
        confirmed = await _confirm_group_id(api, 5, [row], offer_deletion=False)
    finally:
        await api.close()
        await runner.cleanup()
    return confirmed, hits


def test_an_incomplete_row_is_fetched_and_confirmed(capsys: pytest.CaptureFixture) -> None:
    confirmed, hits = anyio.run(_confirm, {"groupId": 5, "groupName": "Row Album"})
    out = capsys.readouterr().out
    assert confirmed is True
    assert hits == ["torrentgroup"]
    assert "Fetched Artist" in out
    assert "Fetched Album" in out


def test_a_complete_row_sends_no_request(capsys: pytest.CaptureFixture) -> None:
    row = {
        "groupId": 5,
        "artist": "Row Artist",
        "groupName": "Row Album",
        "groupYear": 1999,
        "torrents": [{"id": 51, "media": "WEB", "format": "MP3", "encoding": "320"}],
    }
    confirmed, hits = anyio.run(_confirm, row)
    out = capsys.readouterr().out
    assert confirmed is True
    assert hits == []
    assert "Row Artist - Row Album" in out


def test_a_row_whose_torrent_lacks_a_printed_field_is_fetched(capsys: pytest.CaptureFixture) -> None:
    row = {
        "groupId": 5,
        "artist": "Row Artist",
        "groupName": "Row Album",
        "groupYear": 1999,
        "torrents": [{"id": 51, "format": "MP3", "encoding": "320"}],
    }
    confirmed, hits = anyio.run(_confirm, row)
    out = capsys.readouterr().out
    assert confirmed is True
    assert hits == ["torrentgroup"]
    assert "Fetched Album" in out
