"""File and sub-folder names holding a character a tracker refuses (#812).

``BLACKLISTED_CHARS`` used to be applied only to the names salmon builds. ``check_folder_structure``
now renames (or, for a scene release, reports) the names it only moves: logs, cues, images,
sub-folders, and audio files whose rename was declined.
"""

import sys

import anyio
import asyncclick as click
import pytest

from salmon import cfg, interaction
from salmon.errors import NoncompliantFolderStructure
from salmon.tagger.folderstructure import (
    _check_forbidden_characters,
    _forbidden_characters,
    _substitute_forbidden_characters,
    check_folder_structure,
)

# Windows cannot create a file or folder whose name holds any of these characters.
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason='Windows forbids : ? < > \\ * | " in file names')


def _answer(monkeypatch, answer: bool) -> list[str]:
    asked: list[str] = []

    async def confirm(text, **_kwargs):
        asked.append(text)
        return answer

    monkeypatch.setattr(interaction, "confirm", confirm)
    return asked


def _names(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def _release(tmp_path):
    """A release folder with a log holding ':' and a sub-folder holding '?'."""
    root = tmp_path / "Artist - Album [WEB FLAC]"
    sub = root / "Extras?"
    sub.mkdir(parents=True)
    (root / "01 - a:b.log").write_text("log")
    (sub / "02. Plain.flac").write_bytes(b"fLaC")
    return root


def test_forbidden_characters_lists_each_character_once() -> None:
    assert _forbidden_characters("01 - a:b?c:d.flac") == [":", "?"]
    assert _forbidden_characters("01. Plain.flac") == []


def test_substitute_replaces_forbidden_characters_like_generated_names(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "fullwidth_replacements", False)

    assert (
        _substitute_forbidden_characters('a:b?c*d"e<f>g|h\\i/j')
        == f"a{cfg.upload.formatting.blacklisted_substitution}b"
        f"{cfg.upload.formatting.blacklisted_substitution}c"
        f"{cfg.upload.formatting.blacklisted_substitution}d"
        f"{cfg.upload.formatting.blacklisted_substitution}e"
        f"{cfg.upload.formatting.blacklisted_substitution}f"
        f"{cfg.upload.formatting.blacklisted_substitution}g"
        f"{cfg.upload.formatting.blacklisted_substitution}h"
        f"{cfg.upload.formatting.blacklisted_substitution}i"
        f"{cfg.upload.formatting.blacklisted_substitution}j"
    )


def test_substitute_uses_fullwidth_replacements_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "fullwidth_replacements", True)

    assert _substitute_forbidden_characters("a:b?") == "a：b？"


@POSIX_ONLY
def test_check_lists_and_renames_the_offending_file_and_sub_folder(tmp_path, monkeypatch, capsys) -> None:
    root = _release(tmp_path)
    asked = _answer(monkeypatch, True)

    anyio.run(lambda: _check_forbidden_characters(str(root), False))

    assert _names(root) == ["01 - a_b.log", "Extras_", "Extras_/02. Plain.flac"]
    assert len(asked) == 1
    out = capsys.readouterr().out
    assert "01 - a:b.log" in out
    assert "Extras?" in out


@POSIX_ONLY
def test_check_raises_noncompliant_when_the_user_says_no(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)
    before = _names(root)
    _answer(monkeypatch, False)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_forbidden_characters(str(root), False))

    assert _names(root) == before


@POSIX_ONLY
def test_check_does_not_rename_a_scene_release(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)
    before = _names(root)
    asked = _answer(monkeypatch, True)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_forbidden_characters(str(root), True))

    assert _names(root) == before
    assert asked == []


@POSIX_ONLY
def test_check_of_a_scene_release_ends_in_an_abort(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)
    before = _names(root)
    asked = _answer(monkeypatch, True)

    with pytest.raises(click.Abort):
        anyio.run(lambda: check_folder_structure(str(root), True))

    assert _names(root) == before
    assert asked == []


@POSIX_ONLY
def test_check_stops_the_upload_path_until_the_names_are_fixed(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)
    asked = _answer(monkeypatch, True)

    anyio.run(lambda: check_folder_structure(str(root), False))

    assert _names(root) == ["01 - a_b.log", "Extras_", "Extras_/02. Plain.flac"]
    assert len(asked) == 1


@POSIX_ONLY
def test_check_refuses_a_rename_onto_an_existing_path(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)
    (root / "01 - a_b.log").write_text("other")
    before = _names(root)
    asked = _answer(monkeypatch, True)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_forbidden_characters(str(root), False))

    assert _names(root) == before
    assert (root / "01 - a_b.log").read_text() == "other"
    assert asked == []


@POSIX_ONLY
def test_check_leaves_clean_names_alone(tmp_path, monkeypatch) -> None:
    root = tmp_path / "Artist - Album [WEB FLAC]"
    root.mkdir()
    (root / "01. Plain.flac").write_bytes(b"fLaC")
    before = _names(root)
    asked = _answer(monkeypatch, True)

    anyio.run(lambda: check_folder_structure(str(root), False))

    assert _names(root) == before
    assert asked == []
