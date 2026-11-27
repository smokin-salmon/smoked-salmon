"""The user's own notes at the top of every torrent description of a run (#1076).

The runs go against the local fake tracker of helpers.uploader_dry_run, never a real one.
"""

from pathlib import Path
from typing import Any

import anyio
import pytest
from helpers.uploader_dry_run import _album, _run_up
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

from salmon import cfg, interaction
from salmon.uploader.description import ask_torrent_notes, with_notes

pytestmark = pytest.mark.usefixtures("image_uploads")

QUESTION = "Would you like to add notes to the torrent description?"
NOTES = "[b]Ripped by me[/b]\nFrom my own copy."


@pytest.fixture
def torrents(monkeypatch, tmp_path) -> Path:
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return torrents


# The helper


def test_notes_go_on_top_with_one_blank_line() -> None:
    assert with_notes("Encode Specifics", NOTES) == f"{NOTES}\n\nEncode Specifics"


def test_the_editors_trailing_newlines_do_not_widen_the_blank_line() -> None:
    assert with_notes("Encode Specifics", f"{NOTES}\n\n") == f"{NOTES}\n\nEncode Specifics"


@pytest.mark.parametrize("notes", [None, "", "  \n\t "])
def test_no_notes_leave_the_description_as_it_was(notes) -> None:
    assert with_notes("Encode Specifics", notes) == "Encode Specifics"


# The run


def _descriptions(run) -> tuple[list[str], list[str]]:
    """The torrent descriptions and the group descriptions of the uploads the run sent."""
    uploads = [dict(sent.fields) for sent in run.tracker.sent if sent.query.get("action") == "upload" and sent.fields]
    return [u["release_desc"] for u in uploads], [u["album_desc"] for u in uploads if "album_desc" in u]


def _answering(monkeypatch, *, notes: str | None, asked: list[str]) -> None:
    """Answer the notes question yes (no for None notes), the editor with `notes`; other questions as they are."""
    real_confirm = interaction.confirm

    async def confirm(text: str, **kwargs: Any) -> bool:
        if text != QUESTION:
            return await real_confirm(text, **kwargs)
        asked.append(text)
        assert kwargs.get("default") is False
        return notes is not None

    async def edit(text: str, **_kwargs: Any) -> str | None:
        assert text == ""
        return notes

    monkeypatch.setattr(interaction, "confirm", confirm)
    monkeypatch.setattr(interaction, "edit", edit)


def test_the_notes_top_every_torrent_of_a_run_and_never_the_group_description(monkeypatch, tmp_path, torrents) -> None:
    asked: list[str] = []
    _answering(monkeypatch, notes=NOTES, asked=asked)

    # Two trackers, and a FLAC with its two conversions on each.
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        yes_all=False,
        input=INPUT,
        ask_torrent_notes=ask_torrent_notes,
    )

    assert run.result.exit_code == 0, run.result.output
    torrent_descs, group_descs = _descriptions(run)
    assert len(torrent_descs) == 6
    assert all(desc.startswith(f"{NOTES}\n\n") for desc in torrent_descs)
    assert group_descs
    assert not any(NOTES in desc for desc in group_descs)
    assert asked == [QUESTION]


def test_no_notes_leave_every_description_as_it_was(monkeypatch, tmp_path, torrents) -> None:
    asked: list[str] = []
    _answering(monkeypatch, notes="  ", asked=asked)

    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        yes_all=False,
        input=INPUT,
        ask_torrent_notes=ask_torrent_notes,
    )

    assert run.result.exit_code == 0, run.result.output
    torrent_descs, _group = _descriptions(run)
    assert len(torrent_descs) == 6
    assert not any(desc.startswith(" ") or desc.startswith("\n") for desc in torrent_descs)
    assert asked == [QUESTION]


def test_with_defaults_assumed_nothing_is_asked(monkeypatch, tmp_path, torrents) -> None:
    asked: list[str] = []
    _answering(monkeypatch, notes=NOTES, asked=asked)

    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, ask_torrent_notes=ask_torrent_notes)

    assert run.result.exit_code == 0, run.result.output
    torrent_descs, _group = _descriptions(run)
    assert torrent_descs
    assert not any(NOTES in desc for desc in torrent_descs)
    assert asked == []


def test_an_editor_closed_without_saving_gives_no_notes(monkeypatch) -> None:
    async def yes(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def closed(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(interaction, "confirm", yes)
    monkeypatch.setattr(interaction, "edit", closed)

    notes = anyio.run(ask_torrent_notes)

    assert notes is None


INPUT = "\ny\ny\n\ny\n\n\n\ny\n\nn\n"
