"""config.reference.toml documents every setting, and config.default.toml is the short file a new user starts from.

The reference file is kept in sync with the schema here: every key is in it, commented out with its default, and
uncommenting all of them gives a config that loads.
"""

import re
from pathlib import Path

import anyio
import msgspec
import msgspec.inspect as mi
import pytest
from asyncclick.testing import CliRunner
from shipped_config import (  # pyright: ignore[reportMissingImports]
    DEFAULT_CONFIG,
    REFERENCE_CONFIG,
    fake_trackers,
    shipped_config,
)

import salmon.commands
from salmon.commands import checkconf
from salmon.config import Cfg, _parse_config, _schema_keys, _table_members, compat, unknown_keys

_HEADER = re.compile(r"^(?:# )?(\[\[?[\w.]+\]\]?)\s*$")
_COMMENTED_HEADER = re.compile(r"^# (\[\[?[\w.]+\]\]?)\s*$")
_COMMENTED_KEY = re.compile(r"^# ([A-Za-z_]\w*) = (.*)$")
_ACTIVE_KEY = re.compile(r"^[A-Za-z_]\w* = ")


def _reference() -> str:
    return REFERENCE_CONFIG.read_text(encoding="utf-8")


def _uncomment(text: str, music: Path, torrents: Path) -> str:
    """Uncomment every commented header and ``key = value`` line, with real folders and a usable proxy url."""
    folders = {
        "download_directory": f"'{music.as_posix()}'",
        "dottorrents_dir": f"'{torrents.as_posix()}'",
        "tmp_dir": f"'{(music.parent / 'tmp').as_posix()}'",
        "library_dirs": f"['{(music.parent / 'library').as_posix()}']",
        "url": '"socks5://127.0.0.1:1080"',
    }
    out = []
    for line in text.splitlines():
        if m := _COMMENTED_HEADER.match(line):
            line = m.group(1)
        elif m := _COMMENTED_KEY.match(line):
            key, value = m.groups()
            line = f"{key} = {folders.get(key, value)}"
        out.append(line)
    return "\n".join(out) + "\n"


def _folders(tmp_path: Path) -> tuple[Path, Path]:
    music, torrents = tmp_path / "music", tmp_path / "torrents"
    for folder in (music, torrents, tmp_path / "tmp", tmp_path / "library"):
        folder.mkdir()
    return music, torrents


def _short_template(music: Path, torrents: Path) -> str:
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    return text.replace("'.music'", f"'{music.as_posix()}'").replace("'.torrents'", f"'{torrents.as_posix()}'")


def test_every_key_of_the_reference_is_commented_out() -> None:
    assert [line for line in _reference().splitlines() if _ACTIVE_KEY.match(line)] == []


def test_the_reference_uncommented_is_a_valid_config_with_no_unknown_key(tmp_path: Path) -> None:
    music, torrents = _folders(tmp_path)
    text = _uncomment(_reference(), music, torrents)
    raw = msgspec.toml.decode(text.encode())
    assert unknown_keys(raw) == []
    msgspec.convert(raw, type=Cfg)


def test_the_reference_has_every_key_of_the_schema() -> None:
    schema: list[tuple[str, str]] = []
    _schema_keys(_table_members(mi.type_info(Cfg)), "", "", schema)
    header = ""
    documented: set[str] = set()
    for line in _reference().splitlines():
        if m := _HEADER.match(line):
            header = m.group(1).strip("[]")
        elif m := _COMMENTED_KEY.match(line):
            documented.add(f"{header}.{m.group(1)}" if header else m.group(1))
    paths = {path for path, _ in schema}
    assert sorted(paths - documented) == []
    # And nothing it documents is missing from the schema (a typo, or a key since removed).
    assert sorted(documented - paths) == []


def test_the_reference_opens_every_table_except_the_ones_that_need_keys() -> None:
    commented = [m.group(1) for line in _reference().splitlines() if (m := _COMMENTED_HEADER.match(line))]
    assert commented == ["[tracker.red]", "[tracker.ops]", "[tracker.dic]", "[[seedbox]]"]


def test_the_short_template_decodes_with_a_tracker_uncommented(tmp_path: Path) -> None:
    music, torrents = _folders(tmp_path)
    text = _short_template(music, torrents)
    block = "# [tracker.red]\n# session = ''\n# api_key = ''\n"
    assert block in text
    path = tmp_path / "config.toml"
    path.write_text(text.replace(block, "[tracker.red]\nsession = 'cookie'\napi_key = 'key'\n"), encoding="utf-8")
    cfg = _parse_config(path)
    assert cfg.tracker.red is not None and cfg.tracker.red.session == "cookie"
    assert cfg.tracker.ops is None and cfg.tracker.dic is None


def test_the_short_template_as_shipped_needs_a_tracker(tmp_path: Path) -> None:
    music, torrents = _folders(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text(_short_template(music, torrents), encoding="utf-8")
    with pytest.raises(msgspec.ValidationError, match="tracker session cookie"):
        _parse_config(path)


def test_the_short_template_is_short_and_has_no_placeholder_value() -> None:
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 60
    for path, value in compat.PLACEHOLDERS.items():
        assert f"'{value}'" not in text and f'"{value}"' not in text, path


def test_the_short_template_keeps_one_line_per_key_for_the_guided_setup() -> None:
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    for line in ("download_directory = '.music'", "dottorrents_dir = '.torrents'"):
        assert text.count(line) == 1
    for code in ("red", "ops", "dic"):
        assert text.count(f"# [tracker.{code}]\n# session = ''\n") == 1


def test_the_suite_config_is_the_short_template_with_three_fake_trackers(tmp_path: Path) -> None:
    music, torrents = _folders(tmp_path)
    text = shipped_config(music, torrents)
    assert text.endswith(fake_trackers())
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    cfg = _parse_config(path)
    assert [t.session for t in (cfg.tracker.red, cfg.tracker.ops, cfg.tracker.dic) if t] == [
        "test-session-red",
        "test-session-ops",
        "test-session-dic",
    ]


def test_checkconf_reset_copies_the_short_template_and_names_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(salmon.commands, "find_config_path", lambda: path)
    monkeypatch.setattr(salmon.commands, "get_default_config_path", lambda: DEFAULT_CONFIG)

    async def run() -> str:
        result = await CliRunner().invoke(checkconf, ["--reset"])
        assert result.exception is None
        return result.output

    output = anyio.run(run)
    assert path.read_text(encoding="utf-8") == DEFAULT_CONFIG.read_text(encoding="utf-8")
    assert str(REFERENCE_CONFIG) in output
