"""salmon web's Settings page: each tracker's session cookie and API key, shown as set or not, and saved (#1034).

The routes edit a copy of the config in tmp_path, never the one the run loaded. Nothing here reaches a tracker: the
page's connection check after a save is the existing connection_check job, tested in test_webui_connection_check.py.
"""

import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from aiohttp.test_utils import TestClient, TestServer
from helpers.webui_jobs import AUTH, SECRETS, TOKEN

import salmon.config
from salmon.config.edit import ConfigEditError, read_credentials
from salmon.trackers import base
from salmon.webui import server
from salmon.webui import settings as settings_route

RED_TABLE = "\n[tracker.red]\nsession = 'test-session-red'\n"
NEW_COOKIE = "a-new-cookie-0123456789"


@pytest.fixture(autouse=True)
def _no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The redactor masks every secret a tracker client learned in this process (a fake server's short authkey
    "a" among them, which masks the leading "a" of NEW_COOKIE): start from none, and keep these tests' out of others."""
    monkeypatch.setattr(base, "_learned_secrets", set())


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
        # What a header cannot carry, refused before it is saved and every job on the tracker fails.
        ("RED", {"api_key": "a-key-with\na-line-break"}, 422),
        ("RED", {"session": "a cookie-with-a-space"}, 422),
        ("RED", {"session": "a-cookie-\x00-0123456789"}, 422),
        ("RED", {"session": "a-cookie-\u00e9-0123456789"}, 422),
        ("RED", {"session": "x" * 4097}, 422),
        ("RED", {"api_key": "x" * 4097}, 422),
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
        unauthenticated_get = await client.get("/api/settings/trackers")
        assert unauthenticated_get.status == 401

    _with_client(config, test)

    assert config.read_bytes() == before


def test_save_on_an_unparsable_file_is_refused_not_crashed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.write_text("[tracker", encoding="utf-8")

    async def test(client: TestClient, _app: Any) -> None:
        response = await client.post("/api/settings/trackers/RED", json={"session": NEW_COOKIE}, headers=AUTH)
        assert response.status == 422
        assert str(config) in (await response.json())["detail"]

    _with_client(config, test)


# --- From the PR review (#1180) -----------------------------------------------------------


def test_blank_api_key_removes_it(tmp_path: Path) -> None:
    config = _config(tmp_path, "api_key = 'an-old-key-0123456789'")

    async def test(client: TestClient, _app: Any) -> None:
        response = await client.post("/api/settings/trackers/RED", json={"api_key": "   "}, headers=AUTH)
        assert response.status == 200, await response.text()

    _with_client(config, test)

    assert read_credentials(config)["RED"].api_key is None


def test_refused_save_masks_its_value_in_the_error_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A value that was never saved is masked where the save repeats it, and nowhere after."""
    config = _config(tmp_path)

    def refuse(_path: Path, _code: str, update: Any) -> None:
        raise ConfigEditError(f"refused {update.session}")

    monkeypatch.setattr(settings_route, "update_credentials", refuse)

    async def test(client: TestClient, app: Any) -> None:
        response = await client.post("/api/settings/trackers/RED", json={"session": NEW_COOKIE}, headers=AUTH)
        assert response.status == 422
        detail = (await response.json())["detail"]
        assert detail.startswith("refused ")
        assert NEW_COOKIE not in detail
        assert NEW_COOKIE in app[server.REDACTOR].text(f"cookie {NEW_COOKIE} sent")

    _with_client(config, test)
