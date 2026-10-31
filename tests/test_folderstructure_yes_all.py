"""check_folder_structure under -yyy (#821): every question it asks takes its default without asking.

Each test runs the whole check inside ``interaction.assuming_defaults()`` with an interaction that fails
if it is asked anything, and checks what the default did and that it was printed.
"""

import sys

import anyio
import asyncclick as click
import pytest

from salmon import interaction
from salmon.tagger.folderstructure import check_folder_structure

NBSP = " "


class _NoQuestions:
    """An interaction that fails the test if the check asks it anything."""

    async def prompt(self, text, **_kwargs):
        raise AssertionError(f"Asked under -yyy: {text}")

    async def confirm(self, text, **_kwargs):
        raise AssertionError(f"Asked under -yyy: {text}")

    async def edit(self, text, **_kwargs):
        raise AssertionError("Asked to edit under -yyy")

    async def show_spectrals(self, spectrals_path, all_spectral_ids):
        raise AssertionError("Asked to view spectrals under -yyy")

    async def assume_defaults(self) -> bool:
        return interaction.run_assumes_defaults()


def _check_under_yyy(root, **kwargs) -> None:
    with interaction.using(_NoQuestions()), interaction.assuming_defaults():
        anyio.run(lambda: check_folder_structure(str(root), False, **kwargs))


def _names(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def _release(tmp_path):
    root = tmp_path / "Artist - Album [WEB FLAC]"
    root.mkdir()
    (root / "01. Plain.flac").write_bytes(b"fLaC")
    return root


def test_eadir_is_deleted(tmp_path, capsys) -> None:
    root = _release(tmp_path)
    (root / "@eaDir").mkdir()
    (root / "@eaDir" / "thumb.jpg").write_bytes(b"jpg")

    _check_under_yyy(root)

    assert _names(root) == ["01. Plain.flac"]
    assert "-yyy: deleted" in capsys.readouterr().out


def test_names_with_invisible_characters_are_renamed(tmp_path, capsys) -> None:
    root = _release(tmp_path)
    (root / f"02. Some{NBSP}Title.flac").write_bytes(b"fLaC")

    _check_under_yyy(root)

    assert _names(root) == ["01. Plain.flac", "02. Some Title.flac"]
    assert "-yyy: renaming" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason='Windows forbids : ? < > \\ * | " in file names')
def test_names_with_forbidden_characters_are_renamed(tmp_path, capsys) -> None:
    root = _release(tmp_path)
    (root / "02 - a:b.log").write_text("log")

    _check_under_yyy(root)

    assert _names(root) == ["01. Plain.flac", "02 - a_b.log"]
    assert "-yyy: renaming" in capsys.readouterr().out


def test_a_file_with_an_unapproved_extension_is_deleted(tmp_path, capsys) -> None:
    root = _release(tmp_path)
    (root / "notes.xyz").write_text("not part of the release")

    _check_under_yyy(root)

    assert _names(root) == ["01. Plain.flac"]
    assert "-yyy: deleted" in capsys.readouterr().out


def test_several_audio_codecs_abort(tmp_path, capsys) -> None:
    root = _release(tmp_path)
    (root / "02. Other.mp3").write_bytes(b"ID3")

    with pytest.raises(click.Abort):
        _check_under_yyy(root)

    assert _names(root) == ["01. Plain.flac", "02. Other.mp3"]
    assert "-yyy: aborting" in capsys.readouterr().out


def test_a_folder_that_needs_fixing_by_hand_aborts(tmp_path, capsys) -> None:
    root = _release(tmp_path)
    # More than 70 characters over the limit: too long to truncate, so only the user can fix it.
    (root / ("x" * 100 + ".flac")).write_bytes(b"fLaC")
    before = _names(root)

    with pytest.raises(click.Abort):
        _check_under_yyy(root, max_path_length=30)

    assert _names(root) == before
    assert "-yyy: aborting" in capsys.readouterr().out
