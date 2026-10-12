"""A config key the schema does not know is warned about at load time (#681), and the config still loads.

The tests build config files on disk from the shipped template and load them through
``salmon.config._parse_config``, the path an end user's config goes through. ``unknown_keys`` is also called
on untyped tables directly.
"""

import re
from pathlib import Path

import anyio
import msgspec
import pytest
from asyncclick.testing import CliRunner

import salmon.commands
from salmon.commands import checkconf
from salmon.config import Cfg, _parse_config, unknown_keys

_DEFAULT_CONFIG = Path(__file__).parent.parent / "src" / "salmon" / "data" / "config.default.toml"

_SECTION_COMMENT = re.compile(r"^# (\[\[?[\w.]+\]\]?)\s*$")
_KEY_COMMENT = re.compile(r"^# ([A-Za-z_]\w* = .*)$")
_ACTIVE_HEADER = re.compile(r"^\[\[?([\w.]+)\]\]?\s*$")

_REMOVE_SOURCE_DIR_LINE = "# remove_source_dir = false"
_SUGGESTION = "upload.formatting.remove_source_dir (under [upload.formatting])"


def _template(tmp_path: Path) -> str:
    music = tmp_path / "music"
    torrents = tmp_path / "torrents"
    music.mkdir(exist_ok=True)
    torrents.mkdir(exist_ok=True)
    text = _DEFAULT_CONFIG.read_text(encoding="utf-8")
    text = text.replace("download_directory = '.music'", f"download_directory = '{music.as_posix()}'")
    return text.replace("dottorrents_dir = '.torrents'", f"dottorrents_dir = '{torrents.as_posix()}'")


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _load(tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]) -> tuple[Cfg, list[str]]:
    """Load a config from ``text``; return it and the warning lines printed on stderr."""
    path = _write(tmp_path, text)
    capsys.readouterr()
    cfg = _parse_config(path)
    warnings = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    return cfg, warnings


def _uncomment_remove_source_dir(text: str) -> str:
    assert text.count(_REMOVE_SOURCE_DIR_LINE) == 1
    return text.replace(_REMOVE_SOURCE_DIR_LINE, "remove_source_dir = true")


def _table_above(text: str, needle: str) -> str:
    """The last active ``[...]`` or ``[[...]]`` header above ``needle``: where a user's uncommented line lands."""
    table = None
    for line in text.splitlines():
        if line.startswith(needle):
            assert table is not None
            return table
        if m := _ACTIVE_HEADER.match(line):
            table = m.group(1)
    raise AssertionError(f"{needle!r} not in the template")


def test_misplaced_key_in_a_table_is_warned_and_ignored(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = _template(tmp_path) + "\n[upload]\nremove_source_dir = true\n"
    cfg, warnings = _load(tmp_path, text, capsys)
    assert len(warnings) == 1
    assert "Unknown config key upload.remove_source_dir" in warnings[0]
    assert str(tmp_path / "config.toml") in warnings[0]
    assert "ignored" in warnings[0]
    assert f"Did you mean {_SUGGESTION}?" in warnings[0]
    assert cfg.upload.formatting.remove_source_dir is False


def test_top_level_key_from_the_old_config_py_matches_case_insensitively(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "REMOVE_SOURCE_DIR = true\n" + _template(tmp_path)
    cfg, warnings = _load(tmp_path, text, capsys)
    assert len(warnings) == 1
    assert "Unknown config key REMOVE_SOURCE_DIR in" in warnings[0]
    assert _SUGGESTION in warnings[0]
    assert cfg.upload.formatting.remove_source_dir is False


def test_suggestion_for_a_known_top_level_key_names_the_top_of_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _template(tmp_path) + "\n[upload]\ndefault_tracker = 'RED'\n"
    _, warnings = _load(tmp_path, text, capsys)
    assert len(warnings) == 1
    assert "tracker.default_tracker (under [tracker])" in warnings[0]


def test_several_matches_are_each_named_with_their_header(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = _template(tmp_path) + "\n[proxy]\nenabled = true\n"
    _, warnings = _load(tmp_path, text, capsys)
    assert len(warnings) == 1
    assert "upload.ai_review.enabled (under [upload.ai_review])" in warnings[0]
    assert "seedbox.enabled (under [[seedbox]])" in warnings[0]


def test_unknown_keys_are_found_in_arrays_of_tables_and_optional_tables() -> None:
    raw = msgspec.toml.decode(
        b"""
[[seedbox]]
name = "a"

[[seedbox]]
name = "b"
colour = "red"

[image.red]
image_uploader = "catbox"
nonsense = 1

[tracker.red]
session = "x"
other = 2

[tracker.ops]
session = "y"
"""
    )
    assert unknown_keys(raw) == ["seedbox[1].colour", "image.red.nonsense", "tracker.red.other"]


def test_unknown_keys_does_not_descend_into_other_shapes() -> None:
    class Inner(msgspec.Struct):
        known: int = 0

    class Outer(msgspec.Struct):
        free: dict[str, Inner] = {}
        scalar: int = 0
        tables: list[int] = []

    raw = {"free": {"anything": {"whatever": 1}}, "scalar": {"a": 1}, "tables": [{"a": 1}], "extra": 1}
    assert unknown_keys(raw, Outer) == ["extra"]


def test_misspelled_key_without_a_match_gets_no_suggestion(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = _template(tmp_path) + "\n[upload]\nqwertyuiop = true\n"
    _, warnings = _load(tmp_path, text, capsys)
    assert len(warnings) == 1
    assert "Unknown config key upload.qwertyuiop in" in warnings[0]
    assert "Did you mean" not in warnings[0]


def test_a_key_is_warned_about_once_per_process(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = _template(tmp_path) + "\n[upload]\nqwertyuiop = true\n"
    path = _write(tmp_path, text)
    capsys.readouterr()
    _parse_config(path)
    _parse_config(path)
    assert capsys.readouterr().err.count("qwertyuiop") == 1


def test_shipped_template_has_no_unknown_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, warnings = _load(tmp_path, _template(tmp_path), capsys)
    assert warnings == []


def _uncomment_template(text: str) -> str:
    """Uncomment each ``# [section]``, ``# [[section]]`` and single-line ``# key = value`` line, once."""
    out = []
    for line in text.splitlines():
        m = _SECTION_COMMENT.match(line) or _KEY_COMMENT.match(line)
        multi_line = m is not None and m.group(1).rstrip().endswith(("[", '"""', "'''"))
        out.append(m.group(1) if m and not multi_line else line)
    return "\n".join(out) + "\n"


def test_every_commented_template_setting_is_in_the_schema() -> None:
    """A setting the template offers (commented out) must be one the schema reads: the template cannot drift."""
    text = _uncomment_template(_DEFAULT_CONFIG.read_text(encoding="utf-8"))
    raw = msgspec.toml.decode(text.encode())
    assert unknown_keys(raw) == []


def test_support_case_uncommenting_only_remove_source_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Every [upload.*] header is commented out in the template: the line lands under the last active table."""
    template = _template(tmp_path)
    table = _table_above(template, _REMOVE_SOURCE_DIR_LINE)
    cfg, warnings = _load(tmp_path, _uncomment_remove_source_dir(template), capsys)
    assert len(warnings) == 1
    assert f"Unknown config key {table}.remove_source_dir in {tmp_path / 'config.toml'}" in warnings[0]
    assert f"Did you mean {_SUGGESTION}?" in warnings[0]
    assert cfg.upload.formatting.remove_source_dir is False


def test_support_case_with_a_seedbox_enabled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    template = _template(tmp_path)
    assert template.count("# [[seedbox]]") == 1
    text = _uncomment_remove_source_dir(template.replace("# [[seedbox]]", "[[seedbox]]\nname = 'box'"))
    cfg, warnings = _load(tmp_path, text, capsys)
    assert len(warnings) == 1
    assert "Unknown config key seedbox[0].remove_source_dir in" in warnings[0]
    assert f"Did you mean {_SUGGESTION}?" in warnings[0]
    assert cfg.upload.formatting.remove_source_dir is False


def test_checkconf_prints_the_config_file_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, "")
    monkeypatch.setattr(salmon.commands, "find_config_path", lambda: path)
    monkeypatch.setattr(salmon.commands, "_test_seedbox_connections", _no_seedboxes)

    async def run() -> str:
        result = await CliRunner().invoke(checkconf, ["-s"])
        assert result.exception is None
        return result.output

    output = anyio.run(run)
    assert output.splitlines()[0] == f"Config file: {path}"


async def _no_seedboxes() -> None:
    return None
