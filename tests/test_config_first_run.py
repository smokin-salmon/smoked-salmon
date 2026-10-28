"""The guided first run: what salmon does when it finds no config (#761).

The questions are answered by a scripted interaction, never the terminal. The connection check goes to the local
fake tracker of test_checkconf_connection; the network guard in conftest.py stands.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from asyncclick.testing import CliRunner
from test_checkconf_connection import FakeTracker, point_at  # pyright: ignore[reportMissingImports]

import salmon
import salmon.config as config_module
import salmon.trackers
from salmon import cfg, interaction
from salmon.commands import checkconf
from salmon.config import _parse_config, first_run, get_default_config_path
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi

TEMPLATE = get_default_config_path()
# Every character a TOML basic string must escape, and a single quote, which a literal string cannot hold.
NASTY_COOKIE = "a\"b\\c'd\x7fe\tf"


class Scripted:
    """Answers each question from the script, in order, and records it. An exception in the script is raised."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    def _answer(self, kind: str, text: str) -> Any:
        self.asked.append((kind, text))
        assert self.answers, f"asked one question too many: {text!r}"
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def prompt(self, text: str, **kwargs: Any) -> Any:
        return self._answer("prompt", text)

    async def confirm(self, text: str, **kwargs: Any) -> bool:
        return self._answer("confirm", text)

    async def edit(self, text: str, **kwargs: Any) -> str | None:
        return self._answer("edit", text)

    async def show_spectrals(self, spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
        self._answer("show_spectrals", spectrals_path)

    async def assume_defaults(self) -> bool:
        raise AssertionError("the first run has no config to read yes_all from")


@pytest.fixture
def folders(tmp_path: Path) -> tuple[Path, Path]:
    music, torrents = tmp_path / "music", tmp_path / "torrents"
    music.mkdir()
    torrents.mkdir()
    return music, torrents


@pytest.fixture(autouse=True)
def keep_salmon_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection check sets salmon.cfg to the config written: give the test run's back afterwards."""
    monkeypatch.setattr(salmon, "cfg", salmon.cfg)
    monkeypatch.setattr(base, "_learned_secrets", set())


def run_flow(scripted: Scripted, dest: Path) -> int:
    async def run() -> int:
        with interaction.using(scripted):
            return await first_run.run(TEMPLATE, dest)

    return anyio.run(run)


def test_the_answers_make_a_config_that_loads(tmp_path: Path, folders: tuple[Path, Path]) -> None:
    music, torrents = folders
    dest = tmp_path / "config" / "config.toml"
    scripted = Scripted([str(music), str(torrents), "ops, red", NASTY_COOKIE, "", "red-cookie", "red\\key", False])

    exit_code = run_flow(scripted, dest)
    assert exit_code == 0

    assert scripted.answers == []
    assert [text for _, text in scripted.asked] == [
        f"{first_run.FOLDERS[0][1]} (download_directory)",
        f"{first_run.FOLDERS[1][1]} (dottorrents_dir)",
        "Trackers you upload to (RED, OPS, DIC; separate with commas)",
        "OPS session cookie",
        "OPS API key (Enter to skip)",
        "RED session cookie",
        "RED API key (Enter to skip)",
        "Check the connection to OPS, RED now?",
    ]
    loaded = _parse_config(dest)
    assert loaded.directory.download_directory == str(music)
    assert loaded.directory.dottorrents_dir == str(torrents)
    assert loaded.tracker.default_trackers == ["OPS", "RED"]
    assert loaded.tracker.ops is not None and loaded.tracker.ops.session == NASTY_COOKIE
    assert loaded.tracker.ops.api_key is None
    assert loaded.tracker.red is not None and loaded.tracker.red.session == "red-cookie"
    assert loaded.tracker.red.api_key == "red\\key"
    assert loaded.tracker.dic is None


def test_dic_is_asked_no_api_key_and_every_tracker_block_fills_in(tmp_path: Path, folders: tuple[Path, Path]) -> None:
    music, torrents = folders
    dest = tmp_path / "config.toml"
    scripted = Scripted([str(music), str(torrents), "DIC RED OPS", "dic", "red", "", "ops", "ops-key", False])

    exit_code = run_flow(scripted, dest)
    assert exit_code == 0

    assert "DIC API key (Enter to skip)" not in [text for _, text in scripted.asked]
    loaded = _parse_config(dest)
    assert loaded.tracker.default_trackers == ["DIC", "RED", "OPS"]
    assert loaded.tracker.dic is not None and loaded.tracker.dic.session == "dic"
    assert loaded.tracker.dic.api_key is None
    assert loaded.tracker.ops is not None and loaded.tracker.ops.api_key == "ops-key"


def test_an_unknown_or_empty_tracker_answer_is_asked_again(tmp_path: Path, folders: tuple[Path, Path]) -> None:
    music, torrents = folders
    dest = tmp_path / "config.toml"
    scripted = Scripted([str(music), str(torrents), "PTP", " , ", "red", "cookie", "", False])

    exit_code = run_flow(scripted, dest)
    assert exit_code == 0

    assert [text for _, text in scripted.asked].count(
        "Trackers you upload to (RED, OPS, DIC; separate with commas)"
    ) == 3
    assert _parse_config(dest).tracker.default_trackers == ["RED"]


def test_a_missing_folder_is_created_only_when_the_user_agrees(tmp_path: Path, folders: tuple[Path, Path]) -> None:
    music, _ = folders
    declined, created = tmp_path / "declined", tmp_path / "new" / "torrents"
    dest = tmp_path / "config.toml"
    scripted = Scripted([str(declined), False, str(music), str(created), True, "red", "cookie", "", False])

    exit_code = run_flow(scripted, dest)
    assert exit_code == 0

    assert not declined.exists()
    assert created.is_dir()
    assert ("confirm", f"{declined} does not exist. Create it?") in scripted.asked
    loaded = _parse_config(dest)
    assert loaded.directory.download_directory == str(music)
    assert loaded.directory.dottorrents_dir == str(created)


def test_the_folder_defaults_are_the_short_files_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    defaults: list[Any] = []

    class Defaults(Scripted):
        async def prompt(self, text: str, **kwargs: Any) -> Any:
            defaults.append(kwargs.get("default"))
            return await super().prompt(text, **kwargs)

    scripted = Defaults(["", True, "", True, click.Abort()])
    exit_code = run_flow(scripted, tmp_path / "config.toml")
    assert exit_code == 1

    assert defaults[:2] == [str(tmp_path / ".music"), str(tmp_path / ".torrents")]
    assert (tmp_path / ".music").is_dir() and (tmp_path / ".torrents").is_dir()


def test_stopping_part_way_writes_nothing(tmp_path: Path, folders: tuple[Path, Path]) -> None:
    music, _ = folders
    dest = tmp_path / "config.toml"
    scripted = Scripted([str(music), str(tmp_path / "missing"), False, click.Abort()])

    exit_code = run_flow(scripted, dest)
    assert exit_code == 1

    assert not dest.exists()
    assert not (tmp_path / "missing").exists()


def test_an_existing_file_is_never_overwritten(tmp_path: Path, folders: tuple[Path, Path]) -> None:
    music, torrents = folders
    dest = tmp_path / "config.toml"
    dest.write_text("mine", encoding="utf-8")

    exit_code = run_flow(Scripted([str(music), str(torrents), "red", "cookie", ""]), dest)
    assert exit_code == 1
    assert dest.read_text(encoding="utf-8") == "mine"

    with pytest.raises(FileExistsError):
        first_run.copy_and_explain(TEMPLATE, dest)
    assert dest.read_text(encoding="utf-8") == "mine"


REAL_FDOPEN = os.fdopen


class FullDisk:
    """The file os.fdopen gives, on a disk with no space left: every write fails."""

    def __init__(self, fd: int, *args: Any, **kwargs: Any) -> None:
        self.file = REAL_FDOPEN(fd, *args, **kwargs)

    def __enter__(self) -> "FullDisk":
        return self

    def __exit__(self, *exc: object) -> None:
        self.file.close()

    def write(self, text: str) -> int:
        raise OSError(28, "No space left on device")


def test_a_failed_write_leaves_no_file(
    tmp_path: Path, folders: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partial config would load and fail at the next start, instead of the questions being asked again."""
    music, torrents = folders
    written, copied = tmp_path / "written.toml", tmp_path / "copied.toml"
    monkeypatch.setattr(first_run.os, "fdopen", FullDisk)

    exit_code = run_flow(Scripted([str(music), str(torrents), "red", "cookie", ""]), written)
    with pytest.raises(OSError, match="No space left"):
        first_run.copy_and_explain(TEMPLATE, copied)

    assert exit_code == 1
    assert f"Could not write {written}" in capsys.readouterr().out
    assert not written.exists()
    assert not copied.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows ignores the mode")
def test_the_config_is_readable_by_its_owner_only(tmp_path: Path, folders: tuple[Path, Path]) -> None:
    """It holds the tracker cookies: on a shared seedbox, another user must not read them."""
    music, torrents = folders
    written, copied = tmp_path / "written.toml", tmp_path / "copied.toml"

    # A usual umask: a stricter one (0o077) would make any new file 0600, with or without the fix.
    umask = os.umask(0o022)
    try:
        exit_code = run_flow(Scripted([str(music), str(torrents), "red", "cookie", "", False]), written)
        first_run.copy_and_explain(TEMPLATE, copied)
    finally:
        os.umask(umask)

    assert exit_code == 0

    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600


# Through setup_config, as salmon starts


@pytest.fixture
def no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No config anywhere: setup_config makes one at the returned path."""
    dest = tmp_path / "smoked-salmon" / "config.toml"

    def nowhere() -> Path:
        raise FileNotFoundError("Could not find config path")

    monkeypatch.setattr(config_module, "find_config_path", nowhere)
    monkeypatch.setattr(config_module, "get_user_cfg_path", lambda: dest)
    return dest


def test_without_a_terminal_the_short_file_is_copied_and_nothing_asked(
    no_config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(first_run, "is_terminal", lambda: False)

    with interaction.using(Scripted([])), pytest.raises(SystemExit) as exited:
        config_module.setup_config()

    assert exited.value.code == 1
    assert no_config.read_bytes() == TEMPLATE.read_bytes()
    out = capsys.readouterr().out
    assert f"Created a config to fill in at {no_config}" in out
    assert "[tracker.<name>]" in out and "download_directory and dottorrents_dir" in out


def test_in_a_terminal_setup_config_runs_the_questions_and_exits(
    no_config: Path, folders: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    music, torrents = folders
    monkeypatch.setattr(first_run, "is_terminal", lambda: True)
    scripted = Scripted([str(music), str(torrents), "red", "cookie", "", False])

    with interaction.using(scripted), pytest.raises(SystemExit) as exited:
        config_module.setup_config()

    assert exited.value.code == 0
    assert scripted.answers == []
    assert _parse_config(no_config).tracker.red is not None
    out = capsys.readouterr().out
    assert str(TEMPLATE.parent / "config.reference.toml") in out
    assert "Run salmon again." in out


# The connection check


@pytest.fixture
def red_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED alone in the test run's config, with an API key: the check sends its two requests."""
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    assert cfg.tracker.red is not None
    monkeypatch.setattr(cfg.tracker.red, "api_key", "key")


def check_through_flow(
    answer: bool, dest: Path, folders: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeTracker, list[str]]:
    """The first run against a fake RED, answering the connection check's question with ``answer``."""
    music, torrents = folders
    fake = FakeTracker()
    point_at(monkeypatch, {"RED": fake})
    scripted = Scripted([str(music), str(torrents), "red", "cookie", "key", answer])

    async def run() -> None:
        async with fake.serving():
            with interaction.using(scripted):
                exit_code = await first_run.run(TEMPLATE, dest)
                assert exit_code == 0

    anyio.run(run)
    return fake, [text for _, text in scripted.asked]


@pytest.mark.usefixtures("red_with_key")
def test_a_declined_connection_check_sends_nothing(
    tmp_path: Path, folders: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, asked = check_through_flow(False, tmp_path / "config.toml", folders, monkeypatch)

    assert asked[-1] == "Check the connection to RED now?"
    assert fake.sent == []


@pytest.mark.usefixtures("red_with_key")
def test_the_connection_check_sends_what_checkconf_sends_through_request(
    tmp_path: Path, folders: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    through_request: list[str] = []
    real_request = BaseGazelleApi._request

    async def request(self: BaseGazelleApi, *args: Any, **kwargs: Any) -> Any:
        through_request.append(self.site_code)
        return await real_request(self, *args, **kwargs)

    monkeypatch.setattr(BaseGazelleApi, "_request", request)
    fake, _ = check_through_flow(True, tmp_path / "config.toml", folders, monkeypatch)
    assert "Successfully checked RED" in capsys.readouterr().out

    checkconf_fake = FakeTracker()
    point_at(monkeypatch, {"RED": checkconf_fake})

    async def run_checkconf() -> None:
        async with checkconf_fake.serving():
            await CliRunner().invoke(checkconf, ["-t", "RED"])

    anyio.run(run_checkconf)

    assert fake.sent == [("index", "cookie"), ("index", "api key")]
    assert fake.sent == checkconf_fake.sent
    assert through_request == ["RED"] * 4


# Importing before a config exists


def test_the_interaction_layer_and_the_first_run_import_without_a_config() -> None:
    """setup_config runs while salmon/__init__.py is importing, before salmon.cfg exists."""
    script = (
        "import importlib.util, sys\n"
        "spec = importlib.util.find_spec('salmon')\n"
        "sys.modules['salmon'] = importlib.util.module_from_spec(spec)\n"
        "import salmon.interaction, salmon.config.first_run\n"
        "assert not hasattr(sys.modules['salmon'], 'cfg')\n"
        "print('imported')\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "imported"


def test_render_fails_loudly_when_the_short_file_changes() -> None:
    answers = first_run.Answers(
        download_directory="/m", dottorrents_dir="/t", trackers=[first_run.TrackerAnswer(code="RED", session="c")]
    )
    with pytest.raises(ValueError, match="no longer matches"):
        first_run.render("[directory]\ndownload_directory = '.music'\n", answers)
