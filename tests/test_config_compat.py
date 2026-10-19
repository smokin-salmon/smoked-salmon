"""Old config keys and old template values (salmon.config.compat, ADR 0005).

Each test writes a config in its tmp dir and loads it through ``salmon.config._parse_config``, the path a user's
config goes through.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import msgspec
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

import salmon.release_notification
import salmon.trackers.ops
import salmon.trackers.red
from salmon.config import _parse_config, compat
from salmon.config.validations import Cfg
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import dupe_checker

UPGRADE_PAGE = "https://github.com/smokin-salmon/smoked-salmon/wiki/Upgrading-to-0.21"

# Each old key with a value that is not its default, and where the value now lands.
MOVES = [
    ("upload.web_interface.host", "upload.spectrals_viewer.host", "127.0.0.2"),
    ("upload.web_interface.port", "upload.spectrals_viewer.port", 55111),
    ("upload.web_interface.display_host", "upload.spectrals_viewer.display_host", "spectrals.lan"),
    ("upload.debug_tracker_connection", "tracker.debug_connection", True),
    ("upload.description.review_as_comment_tag", "upload.formatting.review_as_comment_tag", False),
    ("upload.description.fullwidth_replacements", "upload.formatting.fullwidth_replacements", True),
    ("upload.description.empty_track_comment_tag", "upload.formatting.empty_track_comment_tag", False),
    ("upload.compression.lma_comment_in_t_desc", "upload.description.lma_comment_in_t_desc", True),
    ("image.auto_compress_cover", "image.embed_missing_cover", True),
]
MOVE_IDS = [old for old, _, _ in MOVES]


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The collected list starts empty: other tests' configs add to it too."""
    monkeypatch.setattr(compat, "config_warnings", [])


def _base(tmp_path: Path) -> dict[str, Any]:
    music, torrents = tmp_path / "music", tmp_path / "torrents"
    music.mkdir(exist_ok=True)
    torrents.mkdir(exist_ok=True)
    return {
        "directory": {"download_directory": music.as_posix(), "dottorrents_dir": torrents.as_posix()},
        "tracker": {"red": {"session": "a-real-cookie"}},
    }


def _put(raw: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    *tables, key = path.split(".")
    node = raw
    for table in tables:
        node = node.setdefault(table, {})
    node[key] = value
    return raw


def _read(cfg: Cfg, path: str) -> Any:
    node: Any = cfg
    for key in path.split("."):
        node = getattr(node, key)
    return node


def _load(tmp_path: Path, raw: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> tuple[Cfg, list[str]]:
    """Load ``raw`` as a config file; return it and the warning lines printed."""
    path = tmp_path / "config.toml"
    path.write_bytes(msgspec.toml.encode(raw))
    capsys.readouterr()
    cfg = _parse_config(path)
    return cfg, [line for line in capsys.readouterr().err.splitlines() if line.strip()]


def _version(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    monkeypatch.setattr(salmon.release_notification, "get_version", lambda: version)


@pytest.mark.parametrize(("old", "new", "value"), MOVES, ids=MOVE_IDS)
def test_an_old_name_sets_the_new_key_and_warns(tmp_path: Path, capsys, old: str, new: str, value: Any) -> None:
    cfg, warnings = _load(tmp_path, _put(_base(tmp_path), old, value), capsys)
    assert _read(cfg, new) == value
    section = new.rpartition(".")[0]
    assert warnings == [
        f"{old} is now {new} (under [{section}]): it still works in 0.21, and stops salmon from 0.22.0. "
        f"See {UPGRADE_PAGE}"
    ]


def test_the_warning_reads_as_the_brief_gives_it(tmp_path: Path, capsys) -> None:
    _, warnings = _load(tmp_path, _put(_base(tmp_path), "upload.debug_tracker_connection", True), capsys)
    assert warnings == [
        "upload.debug_tracker_connection is now tracker.debug_connection (under [tracker]): it still works in "
        "0.21, and stops salmon from 0.22.0. See https://github.com/smokin-salmon/smoked-salmon/wiki/Upgrading-to-0.21"
    ]


@pytest.mark.parametrize(("old", "new", "value"), MOVES, ids=MOVE_IDS)
def test_the_old_and_the_new_name_together_are_refused(tmp_path: Path, capsys, old: str, new: str, value: Any) -> None:
    raw = _put(_put(_base(tmp_path), old, value), new, value)
    with pytest.raises(ValueError, match=f"{old} and {new} are both set"):
        _load(tmp_path, raw, capsys)


@pytest.mark.parametrize(("old", "new", "value"), MOVES, ids=MOVE_IDS)
def test_an_old_name_is_refused_from_0_22_0(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, old: str, new: str, value: Any
) -> None:
    _version(monkeypatch, "0.22.0")
    with pytest.raises(ValueError, match=f"{old} is now {new} .*stops salmon from 0.22.0"):
        _load(tmp_path, _put(_base(tmp_path), old, value), capsys)


@pytest.mark.parametrize("version", ["0.21.9", None], ids=["0.21.9", "unknown version"])
def test_an_old_name_still_works_before_0_22_0(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    _version(monkeypatch, version)
    cfg, _ = _load(tmp_path, _put(_base(tmp_path), "upload.debug_tracker_connection", True), capsys)
    assert cfg.tracker.debug_connection is True


def test_an_old_table_emptied_by_its_moves_is_no_unknown_key(tmp_path: Path, capsys) -> None:
    raw = _base(tmp_path)
    raw["upload"] = {"web_interface": {"host": "127.0.0.2", "port": 55111, "static_root_url": "/static"}}
    cfg, warnings = _load(tmp_path, raw, capsys)
    assert (cfg.upload.spectrals_viewer.host, cfg.upload.spectrals_viewer.port) == ("127.0.0.2", 55111)
    assert not [line for line in warnings if line.startswith("Unknown config key")]


@pytest.mark.parametrize("version", ["0.20.0", "0.22.0", "1.0.0"])
def test_static_root_url_at_its_old_default_is_warned_about_whatever_the_version(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    _version(monkeypatch, version)
    raw = _put(_base(tmp_path), "upload.web_interface.static_root_url", "/static")
    cfg, warnings = _load(tmp_path, raw, capsys)
    assert cfg.upload.spectrals_viewer.port == 55110
    assert warnings == [
        "upload.web_interface.static_root_url is no longer used, remove it: the spectrals viewer now always serves "
        f"its files at /static. See {UPGRADE_PAGE}"
    ]


def test_static_root_url_set_to_anything_else_is_refused(tmp_path: Path, capsys) -> None:
    raw = _put(_base(tmp_path), "upload.web_interface.static_root_url", "/salmon/static")
    with pytest.raises(
        ValueError, match=r"static_root_url is no longer used: .* always serves its files at /static\. Remove it"
    ):
        _load(tmp_path, raw, capsys)


@pytest.mark.parametrize("version", ["0.20.0", "0.22.0", "1.0.0"])
@pytest.mark.parametrize("value", ["your-token", "a-token-from-the-web-player"])
def test_tidal_token_is_warned_about_whatever_its_value_and_version(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, value: str, version: str
) -> None:
    _version(monkeypatch, version)
    cfg, warnings = _load(tmp_path, _put(_base(tmp_path), "metadata.tidal.token", value), capsys)
    assert not hasattr(cfg.metadata.tidal, "token")
    assert len(warnings) == 1
    assert warnings[0].startswith("metadata.tidal.token is no longer used, remove it")
    assert "client_id" in warnings[0] and "client_secret" in warnings[0]


def test_a_placeholder_session_leaves_that_tracker_out(tmp_path: Path, capsys) -> None:
    raw = _base(tmp_path)
    raw["tracker"]["dic"] = {"session": "get-from-site-cookie", "api_key": "x"}
    cfg, warnings = _load(tmp_path, raw, capsys)
    assert cfg.tracker.dic is None
    assert cfg.tracker.red is not None and cfg.tracker.red.session == "a-real-cookie"
    assert len(warnings) == 1
    assert "DIC is left out: set its cookie or remove [tracker.dic]" in warnings[0]


@pytest.mark.parametrize("code", ["red", "ops"])
def test_a_placeholder_session_with_an_api_key_keeps_that_tracker_without_a_cookie(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    raw = _base(tmp_path)
    raw["tracker"][code] = {"session": "get-from-site-cookie", "api_key": "a-real-key"}
    cfg, warnings = _load(tmp_path, raw, capsys)
    kept = getattr(cfg.tracker, code)
    assert kept is not None and (kept.session, kept.api_key) == ("", "a-real-key")
    assert warnings == [
        f"tracker.{code}.session is still the old template's placeholder 'get-from-site-cookie': "
        f"salmon treats it as unset, so {code.upper()} uses only its API key."
    ]
    monkeypatch.setattr(salmon.trackers.red, "cfg", cfg)
    monkeypatch.setattr(salmon.trackers.ops, "cfg", cfg)
    api = salmon.trackers.red.RedApi() if code == "red" else salmon.trackers.ops.OpsApi()
    assert api.api_key == "a-real-key"
    assert api.has_session_cookie is False


@pytest.mark.parametrize("api_key", [None, "", "  "], ids=["no key", "empty key", "blank key"])
def test_a_placeholder_session_without_an_api_key_still_leaves_that_tracker_out(
    tmp_path: Path, capsys, api_key: str | None
) -> None:
    raw = _base(tmp_path)
    raw["tracker"]["ops"] = {"session": "get-from-site-cookie", **({} if api_key is None else {"api_key": api_key})}
    cfg, warnings = _load(tmp_path, raw, capsys)
    assert cfg.tracker.ops is None
    assert "OPS is left out" in warnings[0]


class _LogTracker:
    """A local tracker: the API answers, and log.php (a site page, which needs a cookie) bounces to login.php."""

    def __init__(self) -> None:
        self.hits: list[str] = []

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/ajax.php", self.answer)
        app.router.add_get("/log.php", self.bounce)
        app.router.add_get("/login.php", self.bounce)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def answer(self, request: web.Request) -> web.Response:
        self.hits.append(request.path)
        return web.json_response({"status": "success", "response": {"results": []}})

    async def bounce(self, request: web.Request) -> web.Response:
        self.hits.append(request.path)
        raise web.HTTPFound("/login.php" if request.path == "/log.php" else "/log.php")


async def _a_kept_tracker_without_a_cookie_sends_no_cookie_only_request(api: BaseGazelleApi) -> None:
    tracker = _LogTracker()
    api.base_url = await tracker.start()
    api._rate_limiter = AsyncLimiter(100, 1)
    api._authenticated = True
    try:
        assert dupe_checker.can_check_site_log(api) is False
        await dupe_checker.check_existing_group(api, ["artist title"])
        assert tracker.hits == ["/ajax.php"]
    finally:
        await api.close()
        await tracker.runner.cleanup()


def test_a_kept_tracker_without_a_cookie_skips_cookie_only_requests(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _base(tmp_path)
    raw["tracker"]["red"] = {"session": "get-from-site-cookie", "api_key": "a-real-key"}
    cfg, _ = _load(tmp_path, raw, capsys)
    monkeypatch.setattr(salmon.trackers.red, "cfg", cfg)

    async def new_group(*args, **kwargs) -> str:
        return "n"

    monkeypatch.setattr(dupe_checker.click, "prompt", new_group)
    anyio.run(_a_kept_tracker_without_a_cookie_sends_no_cookie_only_request, salmon.trackers.red.RedApi())
    assert "needs a session cookie" in capsys.readouterr().out


def test_a_placeholder_tracker_is_not_offered(tmp_path: Path) -> None:
    """salmon.trackers.tracker_list, the trackers a run offers, as a fresh salmon builds it from the config."""
    raw = _base(tmp_path)
    raw["tracker"]["ops"] = {"session": "get-from-site-cookie"}
    raw["tracker"]["dic"] = {"session": "another-real-cookie"}
    (tmp_path / "config.toml").write_bytes(msgspec.toml.encode(raw))
    env = {**os.environ, "SALMON_CONFIG_DIR": str(tmp_path)}
    script = "import salmon.trackers; print(salmon.trackers.tracker_list)"
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "['RED', 'DIC']"
    assert "OPS is left out" in result.stderr


def test_every_tracker_a_placeholder_is_no_tracker(tmp_path: Path, capsys) -> None:
    raw = _base(tmp_path)
    raw["tracker"] = {code: {"session": "get-from-site-cookie"} for code in ("red", "ops", "dic")}
    with pytest.raises(msgspec.ValidationError, match="You need a tracker session cookie"):
        _load(tmp_path, raw, capsys)


def test_a_default_tracker_left_out_is_dropped_with_a_warning(tmp_path: Path, capsys) -> None:
    raw = _base(tmp_path)
    raw["tracker"]["ops"] = {"session": "get-from-site-cookie"}
    raw["tracker"]["default_tracker"] = "OPS"
    cfg, warnings = _load(tmp_path, raw, capsys)
    assert cfg.tracker.default_tracker is None
    assert any("tracker.default_tracker = 'OPS' is ignored" in line for line in warnings)


def test_a_default_tracker_kept_stays(tmp_path: Path, capsys) -> None:
    raw = _base(tmp_path)
    raw["tracker"]["ops"] = {"session": "get-from-site-cookie"}
    raw["tracker"]["default_tracker"] = "RED"
    cfg, _ = _load(tmp_path, raw, capsys)
    assert cfg.tracker.default_tracker == "RED"


def test_a_chosen_image_host_with_the_placeholder_key_is_refused(tmp_path: Path, capsys) -> None:
    raw = _base(tmp_path)
    raw["image"] = {"image_uploader": "imgbb", "imgbb_key": "api_key"}
    with pytest.raises(msgspec.ValidationError, match="imgbb key not specified"):
        _load(tmp_path, raw, capsys)


def test_an_unchosen_image_host_with_the_placeholder_key_loads(tmp_path: Path, capsys) -> None:
    raw = _base(tmp_path)
    raw["image"] = {"image_uploader": "catbox", "imgbb_key": "api_key"}
    cfg, warnings = _load(tmp_path, raw, capsys)
    assert cfg.image.imgbb_key is None
    assert len(warnings) == 1
    assert "image.imgbb_key is still the old template's placeholder 'api_key'" in warnings[0]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("metadata.discogs_token", "discogs-token"),
        ("metadata.beatport.username", "username"),
        ("metadata.beatport.password", "password"),
        ("metadata.qobuz.app_id", "app-id"),
        ("metadata.qobuz.user_auth_token", "user_auth_token"),
        ("image.ptscreens_key", "api_key"),
        ("image.oeimg_key", "api_key"),
        ("image.ra_key", "api_key"),
    ],
)
def test_an_old_placeholder_is_unset_with_a_warning(tmp_path: Path, capsys, path: str, value: str) -> None:
    cfg, warnings = _load(tmp_path, _put(_base(tmp_path), path, value), capsys)
    assert _read(cfg, path) is None
    assert len(warnings) == 1
    assert warnings[0].startswith(f"{path} is still the old template's placeholder '{value}'")


def test_a_real_value_is_kept(tmp_path: Path, capsys) -> None:
    cfg, warnings = _load(tmp_path, _put(_base(tmp_path), "metadata.discogs_token", "a-real-token"), capsys)
    assert cfg.metadata.discogs_token == "a-real-token"
    assert warnings == []


def test_the_collected_warnings_hold_the_unknown_keys_too(tmp_path: Path, capsys) -> None:
    raw = _put(_put(_base(tmp_path), "upload.qwertyuiop", True), "upload.debug_tracker_connection", True)
    _, warnings = _load(tmp_path, raw, capsys)
    assert len(warnings) == 2
    assert sorted(compat.config_warnings) == sorted(warnings)
    assert any(line.startswith("Unknown config key upload.qwertyuiop") for line in compat.config_warnings)


def test_a_warning_is_printed_once_per_config_path(tmp_path: Path, capsys) -> None:
    raw = _put(_base(tmp_path), "upload.debug_tracker_connection", True)
    _, first = _load(tmp_path, raw, capsys)
    _, second = _load(tmp_path, raw, capsys)
    assert len(first) == 1
    assert second == []
    assert compat.config_warnings == first
