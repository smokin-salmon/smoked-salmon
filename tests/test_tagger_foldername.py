import os
from pathlib import Path

import anyio
import pytest

from salmon import cfg
from salmon.tagger import foldername


def test_compile_artist_str_excludes_arranger() -> None:
    # Only main artists appear in the folder name, exactly like producer already does.
    artist_str = foldername._compile_artist_str(
        [("Main Artist", "main"), ("Some Arranger", "arranger"), ("Some Producer", "producer")]
    )

    assert artist_str == "Main Artist"


def test_compile_artist_str_keeps_the_order_given() -> None:
    # The folder used to sort the main artists, so "Zed Leader & Alpha Band" became "Alpha Band & Zed Leader".
    artist_str = foldername._compile_artist_str(
        [("Zed Leader", "main"), ("Alpha Band", "main"), ("Zed Leader", "main"), ("Some Guest", "guest")]
    )

    assert artist_str == "Zed Leader & Alpha Band"


def test_compile_artist_str_is_various_at_the_threshold(monkeypatch) -> None:
    # The folder needed one artist more than ALBUMARTIST, so four main artists were "Various" there and named here.
    monkeypatch.setattr(cfg.upload.formatting, "various_artist_threshold", 4)

    artist_str = foldername._compile_artist_str([(name, "main") for name in "ABCD"])

    assert artist_str == cfg.upload.formatting.various_artist_word


def test_hardlink_fallback_survives_a_partial_tree(tmp_path, monkeypatch) -> None:
    # Regression for #356: when os.link fails partway through copytree, the plain-copy
    # fallback used to hit SameFileError on the files that were already hardlinked.
    source = tmp_path / "source_folder"
    source.mkdir()
    file_names = ["a.flac", "b.flac", "c.flac"]
    for i, name in enumerate(file_names):
        (source / name).write_bytes(f"content-{i}".encode())

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    monkeypatch.setattr(cfg.directory, "download_directory", str(download_dir))
    monkeypatch.setattr(cfg.directory, "hardlinks", True)
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", False)

    real_link = os.link
    calls = {"n": 0}

    def flaky_link(src: str, dst: str, *args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("simulated hardlink failure")
        real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", flaky_link)

    metadata = {
        "scene": True,
        "artists": [("Artist", "main")],
        "title": "Title",
        "year": 2024,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
    }

    new_path = anyio.run(lambda: foldername.rename_folder(str(source), metadata, auto_rename=True, check=False))

    assert new_path == str(download_dir / source.name)
    for i, name in enumerate(file_names):
        assert (Path(new_path) / name).read_bytes() == f"content-{i}".encode()


@pytest.mark.parametrize("assume", [False, True], ids=["asked", "-yyy"])
def test_the_new_folder_name_is_accepted_without_asking_under_yyy(monkeypatch, assume) -> None:
    asked: list[str] = []

    async def confirm(text, **_kwargs):
        asked.append(text)
        return True

    monkeypatch.setattr(foldername.interaction, "confirm", confirm)

    with foldername.interaction.assuming_defaults(assume):
        result = anyio.run(lambda: foldername._edit_folder_interactive("New name", False))

    assert result == "New name"
    assert len(asked) == (0 if assume else 1)


_NO_CATNO = {
    "artists": [("A", "main")],
    "title": "T",
    "year": 2020,
    "source": "CD",
    "format": "FLAC",
    "encoding": "Lossless",
    "catno": "",
}


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{artists} - {title} [{source} {catno}]", "A - T [CD]"),
        ("{artists} - {title} [{catno} {source}]", "A - T [CD]"),
        ("{artists} - {title} [{source} {format}] [{catno}]", "A - T [CD FLAC]"),
        ("{artists} - {title} [{source} {catno:>8}]", "A - T [CD]"),
        ("{artists} - {title} ({year}) [{catno}]", "A - T (2020)"),
        ("{artists} - {title} {{{catno}}}", "A - T"),
    ],
    ids=["catno-last", "catno-first", "own-group", "format-spec", "own-group-after-year", "literal-braces"],
)
def test_an_empty_field_leaves_its_group_closed(monkeypatch, template, expected) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", template)

    assert foldername.generate_folder_name(dict(_NO_CATNO)) == expected


def test_an_empty_year_drops_its_whole_group(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", "{artists} - {title} ({year})")

    assert foldername.generate_folder_name({**_NO_CATNO, "year": ""}) == "A - T"
