"""salmon web's Settings page edits each tracker's session and API key in config.toml (#1034).

The tests write config files built from the shipped template into a temporary folder and edit them through
``salmon.config.edit``, as the page's route does. The config the test run loaded is never touched.
"""

import errno
import os
import stat
from pathlib import Path

import pytest
from helpers.shipped_config import shipped_config

from salmon.config import compat
from salmon.config.edit import ConfigEditError, CredentialsUpdate, read_credentials, update_credentials, writable

RED_TABLE = "\n[tracker.red]\nsession = 'test-session-red'\n"


def _config(tmp_path: Path, text: str | None = None) -> Path:
    """A config file in tmp_path: the shipped template, or `text` made from it."""
    music, torrents = tmp_path / "music", tmp_path / "torrents"
    music.mkdir(exist_ok=True)
    torrents.mkdir(exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(text if text is not None else shipped_config(music, torrents), encoding="utf-8")
    return path


def _template(tmp_path: Path) -> str:
    return _config(tmp_path).read_text(encoding="utf-8")


def _with_red(tmp_path: Path, *lines: str) -> Path:
    """The template with these lines added to its [tracker.red] table."""
    text = _template(tmp_path)
    assert text.count(RED_TABLE) == 1
    return _config(tmp_path, text.replace(RED_TABLE, RED_TABLE + "".join(f"{line}\n" for line in lines)))


def _leftovers(tmp_path: Path) -> list[str]:
    return [p.name for p in tmp_path.iterdir() if p.name.startswith(".config.toml.")]


def test_update_keeps_comments_and_layout(tmp_path: Path) -> None:
    text = _template(tmp_path).replace(RED_TABLE, "\n# my note\n" + RED_TABLE.lstrip("\n"))
    path = _config(tmp_path, text)

    update_credentials(path, "RED", CredentialsUpdate(session="new-cookie"))

    assert path.read_text(encoding="utf-8") == text.replace("'test-session-red'", "'new-cookie'")


@pytest.mark.parametrize(
    "table",
    ["[tracker]\nred.session = 'x'\n", "[tracker]\nred = { session = 'x' }\n"],
    ids=["dotted", "inline"],
)
def test_update_in_dotted_and_inline_forms(tmp_path: Path, table: str) -> None:
    text = _template(tmp_path).replace(RED_TABLE, "\n").replace("[tracker]\n", table, 1)
    path = _config(tmp_path, text)
    assert read_credentials(path)["RED"].session == "x"

    update_credentials(path, "RED", CredentialsUpdate(session="new-cookie"))

    assert read_credentials(path)["RED"].session == "new-cookie"
    written = path.read_text(encoding="utf-8")
    # Written where it was, in the same form: only the value differs.
    assert written.replace("new-cookie", "x").replace('"x"', "'x'") == text
    assert "\n[tracker.red]\n" not in written


def test_update_adds_and_clears_api_key(tmp_path: Path) -> None:
    path = _config(tmp_path)

    update_credentials(path, "RED", CredentialsUpdate(api_key="a-key"))
    assert read_credentials(path)["RED"].api_key == "a-key"

    update_credentials(path, "RED", CredentialsUpdate(api_key=None))
    assert read_credentials(path)["RED"].api_key is None
    assert "a-key" not in path.read_text(encoding="utf-8")


def test_invalid_result_is_refused_and_file_untouched(tmp_path: Path) -> None:
    path = _with_red(tmp_path, "api_key = 'a-key'")
    path.write_text(path.read_text(encoding="utf-8") + "\n[image.red]\ncover_uploader = 'red'\n")
    before = path.read_bytes()

    with pytest.raises(ConfigEditError, match=r"needs tracker\.red\.api_key"):
        update_credentials(path, "RED", CredentialsUpdate(api_key=None))

    assert path.read_bytes() == before
    assert _leftovers(tmp_path) == []


def test_missing_tracker_table_is_refused(tmp_path: Path) -> None:
    path = _config(tmp_path, _template(tmp_path).replace("\n[tracker.ops]\nsession = 'test-session-ops'\n", "\n"))

    with pytest.raises(ConfigEditError, match="OPS"):
        update_credentials(path, "OPS", CredentialsUpdate(session="new-cookie"))


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes a read-only file anyway")
def test_unwritable_file_raises_with_path(tmp_path: Path) -> None:
    # The folder stays writable: a rename there would replace a read-only file without a word.
    path = _config(tmp_path)
    path.chmod(0o444)

    assert writable(path) is False
    with pytest.raises(ConfigEditError, match=str(path)):
        update_credentials(path, "RED", CredentialsUpdate(session="new-cookie"))

    assert "new-cookie" not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o444


def test_read_credentials_reports_an_invalid_file(tmp_path: Path) -> None:
    path = _config(tmp_path, "[tracker")

    with pytest.raises(ConfigEditError, match=str(path)):
        read_credentials(path)


def test_read_credentials_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigEditError, match="config.toml"):
        read_credentials(tmp_path / "config.toml")


def test_hand_edit_between_reads_is_kept(tmp_path: Path) -> None:
    path = _config(tmp_path)
    read_credentials(path)
    with path.open("a", encoding="utf-8") as f:
        f.write("# hand edit\n")

    update_credentials(path, "RED", CredentialsUpdate(session="new-cookie"))

    assert "# hand edit" in path.read_text(encoding="utf-8")


def test_update_replaces_atomically_and_keeps_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _config(tmp_path)
    path.chmod(0o640)
    targets: list[str] = []
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        targets.append(str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)

    update_credentials(path, "RED", CredentialsUpdate(session="new-cookie"))

    assert targets == [str(path.resolve())]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert read_credentials(path)["RED"].session == "new-cookie"
    assert _leftovers(tmp_path) == []


def test_update_falls_back_to_in_place_when_rename_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _config(tmp_path)
    path.chmod(0o640)

    def busy(src: str, dst: str) -> None:
        raise OSError(errno.EBUSY, "Device or resource busy", dst)

    monkeypatch.setattr(os, "replace", busy)

    update_credentials(path, "RED", CredentialsUpdate(session="new-cookie"))

    assert read_credentials(path)["RED"].session == "new-cookie"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert _leftovers(tmp_path) == []


def test_update_through_a_symlink_keeps_the_link(tmp_path: Path) -> None:
    path = _config(tmp_path)
    link = tmp_path / "link.toml"
    link.symlink_to(path)

    update_credentials(link, "RED", CredentialsUpdate(session="new-cookie"))

    assert link.is_symlink()
    assert read_credentials(path)["RED"].session == "new-cookie"


def test_reading_prints_no_config_warnings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = (
        _template(tmp_path)
        .replace(RED_TABLE, RED_TABLE + "bogus = 1\n")
        .replace("[tracker]\n", "[tracker]\ndefault_tracker = 'RED'\n", 1)
    )
    path = _config(tmp_path, text)
    warnings_before = list(compat.config_warnings)
    capsys.readouterr()

    read_credentials(path)
    update_credentials(path, "RED", CredentialsUpdate(session="new-cookie"))

    assert capsys.readouterr() == ("", "")
    assert compat.config_warnings == warnings_before
