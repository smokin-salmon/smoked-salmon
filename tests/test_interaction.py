"""The interaction layer: who answers a command's questions, and what `-yyy` is (#630).

The runs go against the local fake tracker of test_uploader_dry_run and a fake image host, never a real one.
"""

from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    _run_up,
    _write_flac,
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

import salmon.trackers
from salmon import cfg, interaction
from salmon.tagger import foldername
from salmon.uploader import spectrals

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")


class Scripted:
    """An implementation that answers from a script and records what it was asked."""

    def __init__(self, answers: list[Any], assumes: bool = False) -> None:
        self.answers = list(answers)
        self.assumes = assumes
        self.asked: list[tuple[str, str]] = []

    async def prompt(self, text: str, **kwargs: Any) -> Any:
        self.asked.append(("prompt", text))
        return self.answers.pop(0)

    async def confirm(self, text: str, **kwargs: Any) -> bool:
        self.asked.append(("confirm", text))
        return self.answers.pop(0)

    async def edit(self, text: str, **kwargs: Any) -> str | None:
        self.asked.append(("edit", text))
        return self.answers.pop(0)

    async def show_spectrals(self, spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
        self.asked.append(("show_spectrals", spectrals_path))

    async def assume_defaults(self) -> bool:
        return self.assumes


# The implementation in use is a context value


def test_an_implementation_set_in_a_context_answers_a_question_deep_in_the_code() -> None:
    scripted = Scripted([False, "A New Name"])

    async def ask() -> str:
        with interaction.using(scripted):
            return await foldername._edit_folder_interactive("Old Name", auto_rename=False)

    assert anyio.run(ask) == "A New Name"
    assert [kind for kind, _ in scripted.asked] == ["confirm", "edit"]
    assert scripted.asked[1][1] == "Old Name"
    assert scripted.answers == []


def test_the_terminal_implementation_is_back_outside_the_context() -> None:
    scripted = Scripted([])
    assert isinstance(interaction.current(), interaction.TerminalInteraction)

    with interaction.using(scripted):
        assert interaction.current() is scripted
    assert isinstance(interaction.current(), interaction.TerminalInteraction)

    async def inside_a_task() -> list[object]:
        seen: list[object] = []

        async def look() -> None:
            seen.append(interaction.current())

        with interaction.using(scripted):
            async with anyio.create_task_group() as group:
                group.start_soon(look)
        seen.append(interaction.current())
        return seen

    seen = anyio.run(inside_a_task)
    assert seen[0] is scripted
    assert isinstance(seen[1], interaction.TerminalInteraction)


def test_two_contexts_do_not_see_each_others_implementation() -> None:
    first, second = Scripted(["one"]), Scripted(["two"])
    answers: dict[str, Any] = {}

    async def job(name: str, implementation: Scripted) -> None:
        with interaction.using(implementation):
            await anyio.sleep(0.01)
            answers[name] = await interaction.prompt("Which?")

    async def both() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(job, "first", first)
            group.start_soon(job, "second", second)

    anyio.run(both)
    assert answers == {"first": "one", "second": "two"}


# The terminal implementation calls asyncclick as the code did


def test_the_terminal_implementation_hands_each_argument_to_asyncclick(monkeypatch) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def prompt(*args: Any, **kwargs: Any) -> str:
        calls.append(("prompt", args, kwargs))
        return "typed"

    def confirm(*args: Any, **kwargs: Any) -> bool:
        calls.append(("confirm", args, kwargs))
        return True

    def edit(*args: Any, **kwargs: Any) -> str:
        calls.append(("edit", args, kwargs))
        return "edited"

    monkeypatch.setattr(click, "prompt", prompt)
    monkeypatch.setattr(click, "confirm", confirm)
    monkeypatch.setattr(click, "edit", edit)

    async def ask() -> list[Any]:
        return [
            await interaction.prompt("Which?", default="a", type=click.STRING, show_default=False),
            await interaction.confirm("Sure?", default=True, abort=True),
            await interaction.edit("text", editor="vim", extension=".json"),
        ]

    assert anyio.run(ask) == ["typed", True, "edited"]
    assert calls == [
        ("prompt", ("Which?",), {"default": "a", "type": click.STRING, "value_proc": None, "show_default": False}),
        ("confirm", ("Sure?",), {"default": True, "abort": True}),
        ("edit", ("text",), {"editor": "vim", "extension": ".json"}),
    ]


def test_the_terminal_viewer_waits_for_enter_through_the_terminal(monkeypatch) -> None:
    viewed: list[tuple[str, dict[int, str]]] = []
    waited: list[str] = []

    async def view_spectrals(path: str, ids: dict[int, str], wait_for_enter: Any) -> None:
        viewed.append((path, ids))
        await wait_for_enter("press enter")

    async def prompt_async(text: str, end: str = "\n", flush: bool = False) -> str:
        waited.append(f"{text}{end}")
        return ""

    monkeypatch.setattr(spectrals, "view_spectrals", view_spectrals)
    monkeypatch.setattr(interaction, "prompt_async", prompt_async)

    anyio.run(interaction.show_spectrals, "/specs", {1: "01.flac"})

    assert viewed == [("/specs", {1: "01.flac"})]
    assert waited == ["press enter "]


# -yyy


def test_assume_defaults_is_a_context_value(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)

    async def read() -> bool:
        return await interaction.assume_defaults()

    assert anyio.run(read) is False
    with interaction.assuming_defaults(False):
        assert anyio.run(read) is False
    with interaction.assuming_defaults():
        assert anyio.run(read) is True
    assert anyio.run(read) is False


def test_the_configured_yes_all_still_assumes_defaults(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", True)

    assert anyio.run(interaction.assume_defaults) is True


def test_an_implementation_decides_for_its_own_context(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)

    async def read() -> list[bool]:
        outside = await interaction.assume_defaults()
        with interaction.using(Scripted([], assumes=True)):
            return [outside, await interaction.assume_defaults()]

    assert anyio.run(read) == [False, True]


@pytest.fixture
def torrents(monkeypatch, tmp_path) -> Path:
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    return torrents


def _album(folder: Path) -> Path:
    folder.mkdir(parents=True)
    _write_flac(folder / "01 - one.flac", title="One", artist="Artist", date="2020")
    _write_flac(folder / "02 - two.flac", title="Two", artist="Artist", date="2020")
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


# A new group, keep the folder name, upload, no lossy report comment, no downconversion.
ANSWERS = "\ny\ny\n\nn\n"


def test_yyy_in_one_run_does_not_leak_into_the_next_run_in_the_process(monkeypatch, tmp_path, torrents) -> None:
    assumed: list[bool] = []

    async def record(*_args: Any, **_kwargs: Any) -> dict:
        assumed.append(await interaction.assume_defaults())
        return {}

    first = _run_up(monkeypatch, _album(tmp_path / "First"), torrents, yes_all=True, check_tags=record)

    assert first.result.exit_code == 0, first.result.output
    assert assumed == [True]
    # -yyy sets nothing in the config: a later run in this process reads the same defaults.
    assert cfg.upload.yes_all is False

    second = _run_up(
        monkeypatch, _album(tmp_path / "Second"), torrents, input=ANSWERS, yes_all=False, check_tags=record
    )

    assert second.result.exit_code == 0, second.result.output
    assert assumed == [True, False]
    # The second run asked its questions: the answers it was given were read.
    assert "Successfully uploaded" in second.result.output
