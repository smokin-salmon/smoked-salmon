"""Two places salmon deletes album files on less than it should (#1005).

The two group questions take only `d`, and ask once more on the terminal before deleting the folder. The folder check
does not delete a file with an unapproved extension from the album itself under -yyy, and its question defaults to
abort there. The files are made-up bytes and the runs go against a local fake tracker.
"""

from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from helpers.interaction_typed_sites import Terminal, ask, confirm_group, recent_group, search_group
from helpers.library_dirs import RENAMED, _run_tag
from helpers.library_dirs import _album as _tag_album
from helpers.uploader_dry_run import _album, _run_up
from test_torrent_file_in_album import (
    MADE_UP,
    Answers,
    dirs,  # noqa: F401 (a fixture)
    image_host,  # noqa: F401 (a fixture)
)

from salmon import cfg, dryrun, interaction
from salmon.errors import AbortAndDeleteFolder
from salmon.tagger.folderstructure import check_folder_structure
from salmon.uploader import _delete_after_abort, spectrals
from salmon.webui.interaction import WebInteraction

SITES = [search_group, recent_group]


@pytest.fixture(autouse=True)
def _interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.image, "default_spectral_ids", None)


# --- The group questions ----------------------------------------------------------------------


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("answer", ["dupe of 1234", "delete", "d "])
def test_a_longer_answer_starting_with_d_is_not_deletion(
    monkeypatch: pytest.MonkeyPatch, site: Callable[[], Callable[[], Awaitable[Any]]], answer: str
) -> None:
    result, terminal, _described = ask(monkeypatch, site(), [answer, "n"])
    assert result is None
    assert len(terminal.calls) == 2


@pytest.mark.parametrize("site", SITES)
def test_as_before_d_is_not_read_when_deletion_is_not_offered(
    monkeypatch: pytest.MonkeyPatch, site: Callable[..., Callable[[], Awaitable[Any]]]
) -> None:
    result, terminal, _described = ask(monkeypatch, site(None, False), ["d", "n"])
    assert result is None
    assert len(terminal.calls) == 2


def _lossy() -> Callable[[], Awaitable[Any]]:
    return partial(spectrals.prompt_lossy_master, False, True, False)


@pytest.mark.parametrize("site", [confirm_group, _lossy])
@pytest.mark.parametrize("answer", ["dupe of 1234", "delete", "d "])
def test_the_other_questions_delete_on_d_alone(
    monkeypatch: pytest.MonkeyPatch, site: Callable[[], Callable[[], Awaitable[Any]]], answer: str
) -> None:
    result, terminal, _described = ask(monkeypatch, site(), [answer, "n"])
    assert result is False
    assert len(terminal.calls) == 2


@pytest.mark.parametrize(
    ("site", "again"), [(search_group, True), (recent_group, True), (confirm_group, False), (_lossy, False)]
)
@pytest.mark.parametrize("answer", ["d", "D"])
def test_d_deletes_and_only_the_group_questions_ask_again(
    monkeypatch: pytest.MonkeyPatch, site: Callable[[], Callable[[], Awaitable[Any]]], again: bool, answer: str
) -> None:
    async def caught(inner: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await inner()
        except AbortAndDeleteFolder as deletion:
            return deletion

    inner = site()
    result, _terminal, _described = ask(monkeypatch, lambda: caught(inner), [answer])
    assert isinstance(result, AbortAndDeleteFolder)
    assert result.ask_again is again


@pytest.mark.parametrize("site", [confirm_group, _lossy])
@pytest.mark.parametrize(("answer", "expected"), [("yes", True), ("nope", False)])
def test_as_before_the_other_questions_read_their_other_answers_by_first_letter(
    monkeypatch: pytest.MonkeyPatch, site: Callable[[], Callable[[], Awaitable[Any]]], answer: str, expected: bool
) -> None:
    result, _terminal, _described = ask(monkeypatch, site(), [answer])
    assert result is expected


# --- Deleting the folder ----------------------------------------------------------------------


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "Artist - Album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"fLaC")
    return folder


def _delete(folder: Path, *, original: str | None = None, ask_again: bool = True) -> None:
    anyio.run(lambda: _delete_after_abort(str(folder), str(folder), original, None, ask_again=ask_again))


def test_the_terminal_keeps_the_folder_on_no(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    folder = _folder(tmp_path)
    source = tmp_path / "The album it was renamed from"
    terminal = Terminal(monkeypatch, [False])

    anyio.run(lambda: _delete_after_abort(str(folder), str(source), None, None, ask_again=True))

    assert folder.exists()
    assert len(terminal.calls) == 1
    kind, text, kwargs = terminal.calls[0]
    assert kind == "confirm"
    assert str(folder) in text
    assert str(source) not in text
    assert kwargs["default"] is False
    assert "Aborting upload" in capsys.readouterr().out


def test_the_terminal_deletes_the_folder_on_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    Terminal(monkeypatch, [True])

    _delete(folder)

    assert not folder.exists()


class _NoQuestions:
    """A browser that fails the test if the job asks it anything."""

    async def ask(self, question: dict[str, Any]) -> Any:
        raise AssertionError(f"Asked: {question}")

    def show_spectrals(self, folder: str, files: list[str]) -> None:
        raise AssertionError("Not a question for this test")


def test_a_web_job_is_not_asked_again(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    with interaction.using(WebInteraction(_NoQuestions())):
        _delete(folder)

    assert not folder.exists()


def test_a_d_typed_under_yyy_is_asked_again_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    terminal = Terminal(monkeypatch, [False])

    with interaction.assuming_defaults():
        _delete(folder)

    assert folder.exists()
    assert len(terminal.calls) == 1


def test_skip_flac_keeps_the_folder_and_asks_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    terminal = Terminal(monkeypatch, [])

    anyio.run(lambda: _delete_after_abort(str(folder), str(folder), None, {}, ask_again=True))

    assert folder.exists()
    assert terminal.calls == []


def test_a_library_folder_is_kept_and_nothing_is_asked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(folder)])
    terminal = Terminal(monkeypatch, [])

    _delete(folder)

    assert folder.exists()
    assert terminal.calls == []


def test_the_other_prompts_do_not_ask_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    terminal = Terminal(monkeypatch, [])

    _delete(folder, ask_again=False)

    assert not folder.exists()
    assert terminal.calls == []


def test_a_dry_run_is_not_asked_and_deletes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    terminal = Terminal(monkeypatch, [])
    monkeypatch.setattr(dryrun, "active", lambda: True)

    _delete(folder)

    assert folder.exists()
    assert terminal.calls == []


class _Terminal(interaction.TerminalInteraction):
    """The terminal, taking each question's default, except no to deleting the folder."""

    async def prompt(self, text: str, **kwargs: Any) -> Any:
        return kwargs["default"]

    async def confirm(self, text: str, **kwargs: Any) -> bool:
        return False if "Delete" in text else kwargs["default"]


@pytest.mark.parametrize("ask_again", [True, False])
def test_the_ask_reaches_the_deletion_from_a_run(
    monkeypatch: pytest.MonkeyPatch,
    dirs: Any,  # noqa: F811
    ask_again: bool,
) -> None:
    _library, downloads, torrents = dirs
    album = _album(downloads / "Album")

    async def stops(*_args: Any, **_kwargs: Any) -> None:
        raise AbortAndDeleteFolder(ask_again=ask_again)

    with interaction.using(_Terminal()):
        run = _run_up(monkeypatch, album, torrents, yes_all=False, _review_until_confirmed=stops)

    assert album.exists() is ask_again, run.result.output


# --- The folder check -------------------------------------------------------------------------


def _release(tmp_path: Path, *strays: str) -> Path:
    root = tmp_path / "Artist - Album [WEB FLAC]"
    root.mkdir()
    (root / "01.flac").write_bytes(b"fLaC")
    for name in strays:
        (root / name).write_bytes(MADE_UP)
    return root


def _names(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def _check(root: Path, answers: Answers, **kwargs: Any) -> None:
    with interaction.using(answers):
        anyio.run(lambda: check_folder_structure(str(root), False, **kwargs))


def _under_yyy(root: Path, **kwargs: Any) -> None:
    with interaction.using(Answers(assumes=True)):
        anyio.run(lambda: check_folder_structure(str(root), False, album_itself=True, **kwargs))


def test_yyy_stops_and_names_every_file_on_the_album_itself(tmp_path: Path, capsys) -> None:
    root = _release(tmp_path, "booklet.zip", "stray.torrent")
    (root / "CD1").mkdir()
    (root / "CD1" / "scan.tiff").write_bytes(MADE_UP)

    with pytest.raises(click.Abort):
        _under_yyy(root)

    assert (root / "booklet.zip").exists()
    assert (root / "CD1" / "scan.tiff").exists()
    out = capsys.readouterr().out
    assert str(root / "CD1" / "scan.tiff") in out
    assert str(root / "booklet.zip") in out
    assert f">> {root / 'stray.torrent'}" not in out


def test_yyy_on_the_album_itself_counts_essential_only(tmp_path: Path, capsys) -> None:
    root = _release(tmp_path, "notes.nfo")

    with pytest.raises(click.Abort):
        _under_yyy(root, essential_only=True)

    assert (root / "notes.nfo").exists()
    assert str(root / "notes.nfo") in capsys.readouterr().out


@pytest.mark.parametrize("name", ["stray.torrent", "._01.flac"])
def test_yyy_deletes_what_cannot_stay_from_the_album_itself(tmp_path: Path, name: str) -> None:
    root = _release(tmp_path, name)

    _under_yyy(root)

    assert _names(root) == ["01.flac"]


def test_the_question_defaults_to_abort_on_the_album_itself(tmp_path: Path) -> None:
    root = _release(tmp_path, "scan.tiff")
    answers = Answers()

    with pytest.raises(click.Abort):
        _check(root, answers, album_itself=True)

    assert (root / "scan.tiff").exists()
    assert "[d]elete, [A]bort, or [c]ontinue?" in answers.questions[0]


def test_delete_and_continue_still_work_on_the_album_itself(tmp_path: Path) -> None:
    root = _release(tmp_path, "scan.tiff", "booklet.zip")
    answers = Answers("d", "c")

    _check(root, answers, album_itself=True)

    assert len(_names(root)) == 2
    assert len(answers.questions) == 2


@pytest.mark.parametrize("name", ["stray.torrent", "._01.flac"])
def test_what_cannot_stay_still_defaults_to_delete_on_the_album_itself(tmp_path: Path, name: str) -> None:
    root = _release(tmp_path, name)
    answers = Answers()

    _check(root, answers, album_itself=True)

    assert _names(root) == ["01.flac"]
    assert "[D]elete or [a]bort" in answers.questions[0]


def test_as_before_the_question_on_a_renamed_copy_defaults_to_delete(tmp_path: Path) -> None:
    root = _release(tmp_path, "scan.tiff")
    answers = Answers()

    _check(root, answers)

    assert not (root / "scan.tiff").exists()
    assert "[D]elete, [a]bort, or [c]ontinue?" in answers.questions[0]


def test_a_scene_release_is_reported_and_not_stopped_as_the_album(tmp_path: Path, capsys) -> None:
    root = _release(tmp_path, "scan.tiff")

    with (
        interaction.using(Answers(assumes=True)),
        pytest.raises(click.Abort),
    ):
        anyio.run(lambda: check_folder_structure(str(root), True, album_itself=True))

    out = capsys.readouterr().out
    assert (root / "scan.tiff").exists()
    assert "manually descene" in out
    assert "-yyy: aborting" not in out


# --- What the runs pass -----------------------------------------------------------------------


def _recording(seen: list[Any]) -> Callable[..., Awaitable[None]]:
    async def check(path: str, scene: bool, **kwargs: Any) -> None:
        seen.append(kwargs.get("album_itself"))

    return check


def _with_booklet(album: Path) -> Path:
    (album / "scan.tiff").write_bytes(MADE_UP)
    return album


def test_an_album_already_named_by_the_template_stops_a_yyy_run(monkeypatch: pytest.MonkeyPatch, dirs: Any) -> None:  # noqa: F811
    _library, downloads, torrents = dirs
    album = _with_booklet(_album(downloads / RENAMED))

    run = _run_up(monkeypatch, album, torrents, check_folder_structure=check_folder_structure)

    assert (album / "scan.tiff").read_bytes() == MADE_UP
    assert str(album / "scan.tiff") in click.unstyle(run.result.output), run.result.output


def test_as_before_a_renamed_copy_loses_the_file_under_yyy(monkeypatch: pytest.MonkeyPatch, dirs: Any) -> None:  # noqa: F811
    _library, downloads, torrents = dirs
    album = _with_booklet(_album(downloads / "Album"))

    run = _run_up(monkeypatch, album, torrents, check_folder_structure=check_folder_structure)

    assert run.result.exit_code == 0, run.result.output
    assert not (downloads / RENAMED / "scan.tiff").exists()
    assert (album / "scan.tiff").read_bytes() == MADE_UP


@pytest.mark.parametrize(
    ("place", "name", "args", "expected"),
    [
        ("downloads", RENAMED, (), True),
        ("downloads", "Album", (), False),
        ("library", RENAMED, (), False),
        # A dry run works on a scratch copy that keeps its name, so the folder is not renamed out of it.
        ("downloads", RENAMED, ("--dry-run",), False),
    ],
)
def test_an_upload_says_whether_the_folder_is_the_album(
    monkeypatch: pytest.MonkeyPatch,
    dirs: Any,  # noqa: F811
    place: str,
    name: str,
    args: tuple[str, ...],
    expected: bool,
) -> None:
    library, downloads, torrents = dirs
    album = _album((library if place == "library" else downloads) / name)
    seen: list[Any] = []

    _run_up(monkeypatch, album, torrents, args, check_folder_structure=_recording(seen))

    assert seen[0] is expected


@pytest.mark.parametrize(("name", "expected"), [(RENAMED, True), ("Album", False)])
def test_salmon_tag_says_whether_the_folder_is_the_album(
    monkeypatch: pytest.MonkeyPatch,
    dirs: Any,  # noqa: F811
    name: str,
    expected: bool,
) -> None:
    _library, downloads, _torrents = dirs
    album = _tag_album(downloads / name)
    seen: list[Any] = []

    _run_tag(monkeypatch, album, check_folder_structure=_recording(seen))

    assert seen == [expected]


def test_a_library_album_at_salmon_tag_is_a_copy(monkeypatch: pytest.MonkeyPatch, dirs: Any) -> None:  # noqa: F811
    library, _downloads, _torrents = dirs
    album = _tag_album(library / RENAMED)
    seen: list[Any] = []

    _run_tag(monkeypatch, album, check_folder_structure=_recording(seen))

    assert seen == [False]
