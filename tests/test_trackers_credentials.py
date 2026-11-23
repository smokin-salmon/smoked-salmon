"""Tracker clients take their session cookie and API key from the running job's snapshot, else the config (#1034).

salmon web reads them from config.toml when a job starts, so a saved change reaches the jobs started after it; the
CLI and anything outside a job use ``cfg`` as before. Nothing here sends a request.
"""

import anyio
import pytest

import salmon.trackers
from salmon import cfg
from salmon.config.edit import Credentials
from salmon.trackers import account
from salmon.trackers.credentials import TrackerRemovedError, secrets_of, using
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi


def test_without_snapshot_clients_use_cfg() -> None:
    assert cfg.tracker.red is not None

    assert RedApi().cookie == cfg.tracker.red.session


def test_snapshot_overrides_cfg() -> None:
    assert cfg.tracker.red is not None

    with using({"RED": Credentials("snap-cookie", "snap-key"), "OPS": Credentials("ops-cookie")}):
        inside = RedApi()
    outside = RedApi()

    assert (inside.cookie, inside.api_key) == ("snap-cookie", "snap-key")
    assert outside.cookie == cfg.tracker.red.session


def test_snapshot_reaches_tasks_started_inside() -> None:
    cookies: list[str] = []

    async def build() -> None:
        cookies.append(OpsApi().cookie)

    async def main() -> None:
        with using({"OPS": Credentials("snap-cookie")}):
            async with anyio.create_task_group() as tg:
                tg.start_soon(build)

    anyio.run(main)

    assert cookies == ["snap-cookie"]


def test_removed_tracker_raises_on_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])

    with using({"OPS": Credentials("ops-cookie")}), pytest.raises(TrackerRemovedError, match="restarts"):
        RedApi()


def test_secrets_of_includes_cookie_forms_and_key() -> None:
    found = secrets_of({"RED": Credentials("a/b+c", "a-key"), "OPS": Credentials("")})

    assert {"a/b+c", "a%2Fb%2Bc", "a-key"} <= set(found)
    assert "" not in found


def test_login_message_points_to_settings_under_salmon_web(monkeypatch: pytest.MonkeyPatch) -> None:
    """An upload catches the LoginError and goes on: the request's own message is what the user reads."""
    assert "Settings page" not in RedApi().login_page_message()

    monkeypatch.setattr(account, "_request_loop", object())

    assert "Settings page" in RedApi().login_page_message()
