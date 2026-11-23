"""salmon web's Settings page: each tracker's session cookie and API key, shown as set or not, and saved (#1034).

The routes edit a copy of the config in tmp_path, never the one the run loaded. Nothing here reaches a tracker: the
page's connection check after a save is the existing connection_check job, tested in test_webui_connection_check.py.
"""

import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from aiohttp.test_utils import TestClient, TestServer
from helpers.webui_jobs import AUTH, SECRETS, TOKEN

import salmon.config
from salmon.config.edit import read_credentials
from salmon.webui import server

RED_TABLE = "\n[tracker.red]\nsession = 'test-session-red'\n"
NEW_COOKIE = "a-new-cookie-0123456789"


def _config(tmp_path: Path, *red_lines: str, session: str = "test-session-red", extra: str = "") -> Path:
    """A copy of the loaded config, with RED's session and these lines in its table."""
    assert salmon.config.loaded_config_path is not None
    text = salmon.config.loaded_config_path.read_text(encoding="utf-8")
    assert text.count(RED_TABLE) == 1
    table = RED_TABLE.replace("test-session-red", session) + "".join(f"{line}\n" for line in red_lines)
    path = tmp_path / "config.toml"
    path.write_text(text.replace(RED_TABLE, table) + extra, encoding="utf-8")
    return path


def _with_client(config: Path, test: Callable[[TestClient, Any], Awaitable[None]]) -> None:
    async def main() -> None:
        app = server.create_app(TOKEN, "127.0.0.1", [], config_path=config)
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            await test(client, app)

    anyio.run(main)


def _tracker(answer: dict[str, Any], code: str) -> dict[str, Any]:
    return next(tracker for tracker in answer["trackers"] if tracker["code"] == code)


def test_get_never_returns_a_secret(tmp_path: Path) -> None:
    config = _config(tmp_path, f"api_key = '{SECRETS['api_key']}'", session=SECRETS["session"])

    async def test(client: TestClient, _app: Any) -> None:
        response = await client.get("/api/settings/trackers", headers=AUTH)
        assert response.status == 200
        text = await response.text()
        assert SECRETS["session"] not in text
        assert SECRETS["api_key"] not in text
        answer = await response.json()
        assert answer["path"] == str(config)
        assert answer["writable"] is True
        assert answer["error"] is None
        assert _tracker(answer, "RED")["session"] == {"set": True, "ends_with": SECRETS["session"][-4:]}
        assert _tracker(answer, "RED")["api_key"] == {"set": True, "ends_with": SECRETS["api_key"][-4:]}
        assert _tracker(answer, "OPS")["api_key"] == {"set": False, "ends_with": None}
        assert _tracker(answer, "DIC")["api_key"] is None

    _with_client(config, test)


def test_short_value_shows_no_ending(tmp_path: Path) -> None:
    config = _config(tmp_path, "api_key = 'short-key1'")

    async def test(client: TestClient, _app: Any) -> None:
        answer = await (await client.get("/api/settings/trackers", headers=AUTH)).json()
        assert _tracker(answer, "RED")["api_key"] == {"set": True, "ends_with": None}

    _with_client(config, test)


def test_get_reports_an_invalid_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.write_text("[tracker", encoding="utf-8")

    async def test(client: TestClient, _app: Any) -> None:
        answer = await (await client.get("/api/settings/trackers", headers=AUTH)).json()
        assert str(config) in answer["error"]
        assert _tracker(answer, "RED")["session"] == {"set": False, "ends_with": None}

    _with_client(config, test)


def test_post_updates_file_and_answers_status(tmp_path: Path) -> None:
    config = _config(tmp_path)

    async def test(client: TestClient, _app: Any) -> None:
        response = await client.post("/api/settings/trackers/red", json={"session": NEW_COOKIE}, headers=AUTH)
        assert response.status == 200, await response.text()
        answer = await response.json()
        assert _tracker(answer, "RED")["session"] == {"set": True, "ends_with": NEW_COOKIE[-4:]}

    _with_client(config, test)

    assert read_credentials(config)["RED"].session == NEW_COOKIE


def test_saved_values_are_masked_from_then_on(tmp_path: Path) -> None:
    config = _config(tmp_path)

    async def test(client: TestClient, app: Any) -> None:
        response = await client.post("/api/settings/trackers/RED", json={"session": NEW_COOKIE}, headers=AUTH)
        assert response.status == 200
        assert NEW_COOKIE not in app[server.REDACTOR].text(f"cookie {NEW_COOKIE} sent")

    _with_client(config, test)


@pytest.mark.parametrize(
    ("code", "body", "status"),
    [
        ("XYZ", {"session": NEW_COOKIE}, 404),
        ("DIC", {"api_key": "a-dic-key-0123456789"}, 422),
        ("RED", {"session": ""}, 422),
        ("RED", {"bogus": 1}, 400),
        ("RED", {"session": 5}, 400),
    ],
)
def test_post_refusals(tmp_path: Path, code: str, body: dict[str, Any], status: int) -> None:
    config = _config(tmp_path)
    before = config.read_bytes()

    async def test(client: TestClient, _app: Any) -> None:
        response = await client.post(f"/api/settings/trackers/{code}", json=body, headers=AUTH)
        assert response.status == status, await response.text()
        assert isinstance((await response.json())["detail"], str)

    _with_client(config, test)

    assert config.read_bytes() == before


def test_invalid_save_is_refused_with_its_reason(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        f"api_key = '{SECRETS['api_key']}'",
        session=SECRETS["session"],
        extra="\n[image.red]\ncover_uploader = 'red'\n",
    )
    before = config.read_bytes()

    async def test(client: TestClient, _app: Any) -> None:
        response = await client.post("/api/settings/trackers/RED", json={"api_key": None}, headers=AUTH)
        assert response.status == 422
        text = await response.text()
        assert "needs tracker.red.api_key" in text
        assert SECRETS["session"] not in text
        assert SECRETS["api_key"] not in text

    _with_client(config, test)

    assert config.read_bytes() == before


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes a read-only file anyway")
def test_read_only_file_reports_unwritable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.chmod(0o444)

    async def test(client: TestClient, _app: Any) -> None:
        answer = await (await client.get("/api/settings/trackers", headers=AUTH)).json()
        assert answer["writable"] is False
        response = await client.post("/api/settings/trackers/RED", json={"session": NEW_COOKIE}, headers=AUTH)
        assert response.status == 422
        assert str(config) in (await response.json())["detail"]

    _with_client(config, test)

    assert stat.S_IMODE(config.stat().st_mode) == 0o444


def test_post_needs_json_and_auth(tmp_path: Path) -> None:
    config = _config(tmp_path)
    before = config.read_bytes()

    async def test(client: TestClient, _app: Any) -> None:
        unauthenticated = await client.post("/api/settings/trackers/RED", json={"session": NEW_COOKIE})
        assert unauthenticated.status == 401
        form = await client.post("/api/settings/trackers/RED", data={"session": NEW_COOKIE}, headers=AUTH)
        assert form.status == 415
        assert (await client.get("/api/settings/trackers")).status == 401

    _with_client(config, test)

    assert config.read_bytes() == before
