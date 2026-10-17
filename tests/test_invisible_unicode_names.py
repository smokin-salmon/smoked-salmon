"""Names with invisible or odd Unicode characters (#672): the tracker refuses such a torrent.

Generated folder and file names are cleaned, and check_folder_structure renames (or, for a scene
release, reports) the names that still hold one.
"""

import os
from types import SimpleNamespace

import anyio
import asyncclick as click
import pytest

from salmon import cfg, interaction
from salmon.common import clean_name, invisible_chars
from salmon.errors import NoncompliantFolderStructure, UploadError
from salmon.tagger.foldername import _edit_folder_interactive, generate_folder_name, rename_folder
from salmon.tagger.folderstructure import _check_invisible_characters, check_folder_structure
from salmon.tagger.retagger import generate_file_name

NBSP = " "
ZWSP = "​"
BOM = "﻿"
WORD_JOINER = "⁠"


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
    """A release folder with a file and a sub-folder whose names hold a no-break space or a word joiner."""
    root = tmp_path / "Artist - Album [WEB FLAC]"
    sub = root / f"Disc{WORD_JOINER} 1"
    sub.mkdir(parents=True)
    (root / f"01. Some{NBSP}Title.flac").write_bytes(b"fLaC")
    (sub / "02. Plain.flac").write_bytes(b"fLaC")
    return root


# clean_name and invisible_chars


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (f"Some{NBSP}Title", "Some Title"),
        (f"a{ZWSP}b{BOM}c{WORD_JOINER}d­E", "abcdE"),
        ("a b c d　e f g", "a b c d e f g"),
        ("a‌b‍c‎d", "abcd"),
        ("a\tb\nc", "abc"),
        (f"  a{NBSP}{NBSP}  b {ZWSP}", "a b"),
    ],
)
def test_clean_name_replaces_spaces_and_removes_format_and_control_characters(name, expected) -> None:
    assert clean_name(name) == expected


def test_clean_name_keeps_the_original_when_nothing_would_be_left() -> None:
    assert clean_name(f"{ZWSP}{NBSP}") == f"{ZWSP}{NBSP}"


@pytest.mark.parametrize(
    "name",
    ["Café Ünïcode", "日本語のタイトル", "ｆｕｌｌｗｉｄｔｈ！", "Song 🎵 Title 🎧", "Mixed - (feat. X) [2024]"],
)
def test_clean_name_leaves_ordinary_names_alone(name) -> None:
    assert clean_name(name) == name
    assert invisible_chars(name) == []


def test_invisible_chars_names_each_character_once() -> None:
    assert invisible_chars(f"a{NBSP}b{NBSP}c{ZWSP}d\t") == [
        "U+00A0 NO-BREAK SPACE",
        "U+200B ZERO WIDTH SPACE",
        "U+0009 CONTROL CHARACTER",
    ]


# Generated names


def _tags():
    return SimpleNamespace(
        artist=["Some Artist"],
        title=f"Title{NBSP}One{ZWSP}{BOM}",
        tracknumber="1",
        discnumber="1",
    )


def _file_template(monkeypatch):
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}. {title}")
    monkeypatch.setattr(cfg.upload.formatting, "no_artist_in_filename_if_only_one_album_artist", False)


def test_generate_file_name_replaces_and_removes_invisible_characters(monkeypatch) -> None:
    _file_template(monkeypatch)

    assert generate_file_name(_tags(), ".flac", multiple_artists=False) == "01. Title One.flac"


def test_generate_file_name_does_not_leave_a_space_before_the_extension(monkeypatch) -> None:
    _file_template(monkeypatch)
    tags = _tags()
    tags.title = f"Title{NBSP}"

    assert generate_file_name(tags, ".flac", multiple_artists=False) == "01. Title.flac"


def test_generate_folder_name_replaces_and_removes_invisible_characters(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", "{artists} - {title} ({year})")
    metadata = {
        "artists": [(f"An{NBSP}Artist", "main")],
        "title": f"The{NBSP}Album{ZWSP}{BOM}",
        "year": 2024,
    }

    assert generate_folder_name(metadata) == "An Artist - The Album (2024)"


@pytest.mark.parametrize("replacements", [True, False])
def test_generated_names_keep_fullwidth_replacements_accents_and_emoji(monkeypatch, replacements) -> None:
    monkeypatch.setattr(cfg.upload.description, "fullwidth_replacements", replacements)
    _file_template(monkeypatch)
    tags = _tags()
    tags.title = "Café: 日本語 \U0001f3b5 What?"

    name = generate_file_name(tags, ".flac", multiple_artists=False)

    sub = "：" if replacements else cfg.upload.formatting.blacklisted_substitution
    question = "？" if replacements else cfg.upload.formatting.blacklisted_substitution
    assert name == f"01. Café{sub} 日本語 \U0001f3b5 What{question}.flac"


# check_folder_structure


def test_check_lists_and_renames_the_offending_file_and_sub_folder(tmp_path, monkeypatch, capsys) -> None:
    root = _release(tmp_path)
    asked = _answer(monkeypatch, True)

    anyio.run(lambda: check_folder_structure(str(root), False))

    assert _names(root) == ["01. Some Title.flac", "Disc 1", "Disc 1/02. Plain.flac"]
    assert len(asked) == 1
    out = capsys.readouterr().out
    assert "U+00A0 NO-BREAK SPACE" in out
    assert "U+2060 WORD JOINER" in out


def test_check_raises_noncompliant_when_the_user_says_no(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)
    before = _names(root)
    _answer(monkeypatch, False)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_invisible_characters(str(root), False))

    assert _names(root) == before


def test_check_does_not_rename_a_scene_release(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)
    before = _names(root)
    asked = _answer(monkeypatch, True)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_invisible_characters(str(root), True))

    assert _names(root) == before
    assert asked == []


def test_check_of_a_scene_release_ends_in_an_abort(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)
    before = _names(root)
    asked = _answer(monkeypatch, True)

    with pytest.raises(click.Abort):
        anyio.run(lambda: check_folder_structure(str(root), True))

    assert _names(root) == before
    assert asked == []


def test_check_refuses_a_rename_onto_an_existing_path(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)
    (root / "01. Some Title.flac").write_bytes(b"other")
    before = _names(root)
    asked = _answer(monkeypatch, True)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_invisible_characters(str(root), False))

    assert _names(root) == before
    assert (root / "01. Some Title.flac").read_bytes() == b"other"
    assert asked == []


def test_check_refuses_two_names_that_clean_to_the_same_name(tmp_path, monkeypatch) -> None:
    root = tmp_path / "Release"
    root.mkdir()
    (root / f"a{NBSP}b.flac").write_bytes(b"1")
    (root / f"a{ZWSP}{NBSP}b.flac").write_bytes(b"2")
    _answer(monkeypatch, True)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_invisible_characters(str(root), False))

    assert len(_names(root)) == 2


def test_check_refuses_a_name_with_nothing_left_after_cleaning(tmp_path, monkeypatch) -> None:
    root = tmp_path / "Release"
    root.mkdir()
    (root / f"{ZWSP}{NBSP}").write_bytes(b"1")
    asked = _answer(monkeypatch, True)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_invisible_characters(str(root), False))

    assert len(_names(root)) == 1
    assert asked == []


def test_check_aborts_on_a_release_folder_name_it_cannot_rename(tmp_path, monkeypatch) -> None:
    root = tmp_path / f"Artist{NBSP}- Album"
    root.mkdir()
    (root / "01. Title.flac").write_bytes(b"fLaC")
    asked = _answer(monkeypatch, True)

    with pytest.raises(click.Abort):
        anyio.run(lambda: _check_invisible_characters(str(root), False))

    assert root.is_dir()
    assert asked == []


def test_check_leaves_clean_names_alone(tmp_path, monkeypatch) -> None:
    root = tmp_path / "Artist - Café 日本語 \U0001f3b5"
    root.mkdir()
    (root / "01. Ｔｉｔｌｅ： Café.flac").write_bytes(b"fLaC")
    asked = _answer(monkeypatch, True)
    before = _names(root)

    anyio.run(lambda: check_folder_structure(str(root), False))

    assert _names(root) == before
    assert asked == []


def test_path_length_check_counts_the_cleaned_name(tmp_path, monkeypatch) -> None:
    # 30 zero-width characters in the name: the path is over the limit only while they are counted.
    root = tmp_path / "F"
    root.mkdir()
    (root / f"{'x' * 150}{ZWSP * 30}.flac").write_bytes(b"fLaC")
    _answer(monkeypatch, True)

    anyio.run(lambda: check_folder_structure(str(root), False, max_path_length=180))

    assert _names(root) == [f"{'x' * 150}.flac"]


# rename_folder and generate_file_name refuse a name that shows nothing or escapes its directory

FOLDER_METADATA = {
    "scene": False,
    "title": "Album",
    "artists": [("Artist", "main")],
    "year": 2020,
    "source": "WEB",
    "format": "FLAC",
    "encoding": "Lossless",
}


def _download_dir(tmp_path, monkeypatch):
    """A download directory with a file in it, below a parent holding a sibling file."""
    parent = tmp_path / "parent"
    downloads = parent / "downloads"
    downloads.mkdir(parents=True)
    (parent / "sibling.txt").write_text("keep")
    (downloads / "other.txt").write_text("keep")
    album = downloads / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"fLaC")
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    return parent, downloads, album


def _assert_untouched(parent, downloads, album) -> None:
    assert (parent / "sibling.txt").read_text() == "keep"
    assert (downloads / "other.txt").read_text() == "keep"
    assert (album / "01.flac").read_bytes() == b"fLaC"


def _default_answers(monkeypatch) -> list[str]:
    """Every question answered with its default, as -yyy does."""
    asked: list[str] = []

    async def confirm(text, default=False, **_kwargs):
        asked.append(text)
        return default

    monkeypatch.setattr(interaction, "confirm", confirm)
    return asked


@pytest.mark.parametrize("name", ["", ".", ".."])
def test_rename_folder_refuses_a_name_that_is_not_a_folder_name(tmp_path, monkeypatch, name) -> None:
    parent, downloads, album = _download_dir(tmp_path, monkeypatch)
    monkeypatch.setattr("salmon.tagger.foldername.generate_folder_name", lambda _metadata: name)
    _default_answers(monkeypatch)

    # The default answer to "replace the existing folder?" is yes, and that used to rmtree the directory.
    with pytest.raises(UploadError, match="Invalid folder name"):
        anyio.run(lambda: rename_folder(str(album), FOLDER_METADATA, auto_rename=True))

    _assert_untouched(parent, downloads, album)


@pytest.mark.parametrize("name", ["", ".", ".."])
def test_rename_folder_without_check_refuses_a_name_that_is_not_a_folder_name(tmp_path, monkeypatch, name) -> None:
    parent, downloads, album = _download_dir(tmp_path, monkeypatch)
    monkeypatch.setattr("salmon.tagger.foldername.generate_folder_name", lambda _metadata: name)
    _default_answers(monkeypatch)

    with pytest.raises(UploadError, match="Invalid folder name"):
        anyio.run(lambda: rename_folder(str(album), FOLDER_METADATA, auto_rename=True, check=False))

    _assert_untouched(parent, downloads, album)


@pytest.mark.parametrize("name", ["", ".", ".."])
def test_rename_folder_refuses_an_edited_name_that_is_not_a_folder_name(tmp_path, monkeypatch, name) -> None:
    parent, downloads, album = _download_dir(tmp_path, monkeypatch)
    asked: list[str] = []

    async def confirm(text, default=False, **_kwargs):
        asked.append(text)
        # The new name is not acceptable (so it is edited), the retry is declined, the rest take the default.
        return default and "acceptable" not in text and "retry" not in text

    async def edit(*_args, **_kwargs):
        return name

    monkeypatch.setattr(interaction, "confirm", confirm)
    monkeypatch.setattr(interaction, "edit", edit)

    with pytest.raises(SystemExit):
        anyio.run(lambda: rename_folder(str(album), FOLDER_METADATA, auto_rename=False))

    assert any("retry" in text for text in asked)
    _assert_untouched(parent, downloads, album)


def test_rename_folder_refuses_a_template_that_renders_empty(tmp_path, monkeypatch) -> None:
    parent, downloads, album = _download_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", "{description}")
    _default_answers(monkeypatch)

    with pytest.raises(UploadError, match="Invalid folder name"):
        anyio.run(
            lambda: rename_folder(str(album), {**FOLDER_METADATA, "description": ""}, auto_rename=True, check=False)
        )

    _assert_untouched(parent, downloads, album)


@pytest.mark.parametrize("title", [ZWSP, f"{NBSP}{BOM}"])
def test_rename_folder_refuses_a_name_with_nothing_visible(tmp_path, monkeypatch, title) -> None:
    parent, downloads, album = _download_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", "{title}")
    _default_answers(monkeypatch)

    with pytest.raises(UploadError, match="Invalid folder name"):
        anyio.run(lambda: rename_folder(str(album), {**FOLDER_METADATA, "title": title}, auto_rename=True, check=False))

    _assert_untouched(parent, downloads, album)


@pytest.mark.parametrize("name", ["..", ZWSP, ""])
def test_edit_folder_offers_a_retry_for_a_name_that_would_be_refused(monkeypatch, name) -> None:
    names = iter([name, "Fixed Name"])

    async def edit(*_args, **_kwargs):
        return next(names)

    async def confirm(text, default=False, **_kwargs):
        return "acceptable" not in text

    monkeypatch.setattr(interaction, "edit", edit)
    monkeypatch.setattr(interaction, "confirm", confirm)

    assert anyio.run(lambda: _edit_folder_interactive("Old", auto_rename=False)) == "Fixed Name"


def test_rename_folder_still_renames_a_normal_name(tmp_path, monkeypatch) -> None:
    parent, downloads, album = _download_dir(tmp_path, monkeypatch)
    _default_answers(monkeypatch)

    new_path = anyio.run(lambda: rename_folder(str(album), FOLDER_METADATA, auto_rename=True, check=False))

    assert new_path.startswith(str(downloads))
    assert new_path != str(album)
    assert (downloads / "other.txt").read_text() == "keep"


@pytest.mark.parametrize("title", [ZWSP, f"{NBSP}{BOM}", ""])
def test_generate_file_name_refuses_a_name_with_nothing_visible(monkeypatch, title) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{title}")
    tags = _tags()
    tags.title = title

    with pytest.raises(UploadError, match="nothing visible in file_template"):
        generate_file_name(tags, ".flac", multiple_artists=False)


@pytest.mark.parametrize("name", ["a/b", "../escape", "/abs"])
def test_rename_folder_refuses_a_name_with_a_path_separator(tmp_path, monkeypatch, name) -> None:
    parent, downloads, album = _download_dir(tmp_path, monkeypatch)
    monkeypatch.setattr("salmon.tagger.foldername.generate_folder_name", lambda _metadata: name)
    _default_answers(monkeypatch)

    with pytest.raises(UploadError, match="Invalid folder name"):
        anyio.run(lambda: rename_folder(str(album), FOLDER_METADATA, auto_rename=True, check=False))

    _assert_untouched(parent, downloads, album)
    assert not (parent / "escape").exists()


def test_check_stops_when_a_new_name_is_taken_after_the_check(tmp_path, monkeypatch) -> None:
    root = _release(tmp_path)

    async def confirm(*_args, **_kwargs):
        (root / "01. Some Title.flac").write_bytes(b"other")
        return True

    monkeypatch.setattr(interaction, "confirm", confirm)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_invisible_characters(str(root), False))

    assert (root / "01. Some Title.flac").read_bytes() == b"other"
    assert (root / f"01. Some{NBSP}Title.flac").read_bytes() == b"fLaC"


def test_check_stops_at_the_first_failed_rename_and_names_the_names_left(tmp_path, monkeypatch, capsys) -> None:
    root = _release(tmp_path)
    _answer(monkeypatch, True)

    def failing_rename(src, dst):
        raise PermissionError(13, "Permission denied", src)

    monkeypatch.setattr(os, "rename", failing_rename)

    with pytest.raises(NoncompliantFolderStructure):
        anyio.run(lambda: _check_invisible_characters(str(root), False))

    out = capsys.readouterr().out
    assert "Permission denied" in out
    assert "Not renamed" in out
    assert f"Disc{WORD_JOINER} 1" in out
