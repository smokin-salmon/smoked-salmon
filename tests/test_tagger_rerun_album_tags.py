"""A re-run of the metadata step clears the album tags an earlier pass wrote that the new metadata no longer has
(#835)."""

from functools import partial
from typing import Any

import anyio
import asyncclick as click
import msgspec
import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TRCK
from mutagen.mp4 import MP4
from test_interaction_typed_sites import ask, edit_metadata_stubs  # pyright: ignore[reportMissingImports]
from test_tagger_multi_value_tags import (  # pyright: ignore[reportMissingImports]
    _gather,
    _metadata,
    _trackmeta,
    _write_flac,
    _write_m4a,
    _write_mp3,
)

import salmon.uploader as uploader
from salmon import cfg, interaction
from salmon.tagger.retagger import tag_files

ARTISTS = [("Dree Low", "main")]


@pytest.fixture(autouse=True)
def _formatting(monkeypatch):
    monkeypatch.setattr(
        cfg.upload,
        "formatting",
        msgspec.structs.replace(cfg.upload.formatting, guests_in_track_title=False, review_as_comment_tag=True),
    )


def _flac(tmp_path, name: str = "01.flac", number: str = "1", **own: str) -> None:
    _write_flac(tmp_path / name, TRACKNUMBER=number, DISCNUMBER="1", TITLE="Song", ARTIST="Dree Low", **own)


def _release(tracks: int = 1, **album: Any) -> dict[str, Any]:
    songs = [_trackmeta(ARTISTS, number=str(n)) for n in range(1, tracks + 1)]
    return {**_metadata(*songs, genres=()), "scene": False, **album}


def _catno(path) -> list[str] | None:
    return FLAC(path).get("catalognumber")


def _pass(
    monkeypatch, capsys, tmp_path, written: dict[str, Any], *, apply: bool = True, tracks: int = 1, **album: Any
) -> list[list[str]]:
    """One pass of tag_files over the folder with this album metadata, answering the retag question with `apply`.

    Returns the album tag lines it printed, split into words.
    """

    async def confirm(*_args: Any, **_kwargs: Any) -> bool:
        return apply

    monkeypatch.setattr(interaction, "confirm", confirm)
    capsys.readouterr()
    metadata = _release(tracks, **album)
    anyio.run(lambda: tag_files(str(tmp_path), _gather(tmp_path), metadata, False, written=written))
    album_lines = click.unstyle(capsys.readouterr().out).split("Album tags (applied to all):")[-1]
    return [line.split() for line in album_lines.splitlines() if line.startswith(">")]


@pytest.mark.parametrize(
    ("kind", "field", "album", "emptied", "held"),
    [
        ("flac", "catno", {"catno": "CAT1"}, {"catno": None}, lambda path: FLAC(path).get("catalognumber")),
        ("mp3", "label", {"label": "Label"}, {"label": None}, lambda path: ID3(path).getall("TPUB")),
        # salmon reads its own MP3 comment back as None: the line shows the value the first pass wrote.
        ("mp3", "comment", {"comment": "Review"}, {"comment": None}, lambda path: ID3(path).getall("COMM")),
        ("m4a", "genre", {"genres": {"Pop"}}, {"genres": set()}, lambda path: (MP4(path).tags or {}).get("\xa9gen")),
    ],
    ids=["flac-catno", "mp3-label", "mp3-comment", "m4a-genre"],
)
def test_a_rerun_clears_an_album_tag_the_first_pass_wrote(
    monkeypatch, capsys, tmp_path, kind, field, album, emptied, held
) -> None:
    path = tmp_path / f"01.{kind}"
    if kind == "flac":
        _flac(tmp_path)
    elif kind == "mp3":
        _write_mp3(path, TRCK(encoding=3, text="1"))
    else:
        _write_m4a(path, trkn=[(1, 0)])
    written: dict[str, Any] = {}
    (value,) = album.values()

    _pass(monkeypatch, capsys, tmp_path, written, **album)
    assert held(path)
    # The second review empties the field: the proposed changes say so, and the files no longer hold it.
    lines = _pass(monkeypatch, capsys, tmp_path, written, **emptied)

    shown = sorted(value) if isinstance(value, set) else [value]
    assert [line for line in lines if line[1] == field] == [[">", field, "•••", *shown, ">>>", "None"]]
    assert not held(path)


def test_a_rerun_clears_a_tag_the_files_held_before_the_run(monkeypatch, capsys, tmp_path) -> None:
    # The first pass wrote the catalogue number the files already had: emptied at the re-run's review, it goes.
    _flac(tmp_path, CATALOGNUMBER="CAT1")
    written: dict[str, Any] = {}

    _pass(monkeypatch, capsys, tmp_path, written, catno="CAT1")
    _pass(monkeypatch, capsys, tmp_path, written, catno=None)

    assert _catno(tmp_path / "01.flac") is None


def test_a_pass_whose_retag_was_declined_wrote_nothing(monkeypatch, capsys, tmp_path) -> None:
    _flac(tmp_path, CATALOGNUMBER="CAT0")
    written: dict[str, Any] = {}

    _pass(monkeypatch, capsys, tmp_path, written, apply=False, catno="CAT1")
    _pass(monkeypatch, capsys, tmp_path, written, catno=None)

    assert _catno(tmp_path / "01.flac") == ["CAT0"]


def test_a_declined_rerun_leaves_the_tag_for_a_later_pass_to_clear(monkeypatch, capsys, tmp_path) -> None:
    _flac(tmp_path)
    written: dict[str, Any] = {}

    _pass(monkeypatch, capsys, tmp_path, written, catno="CAT1")
    _pass(monkeypatch, capsys, tmp_path, written, apply=False, catno=None)
    assert _catno(tmp_path / "01.flac") == ["CAT1"]
    _pass(monkeypatch, capsys, tmp_path, written, catno=None)

    assert _catno(tmp_path / "01.flac") is None


def test_a_tag_the_format_has_no_field_for_is_not_offered_for_clearing(monkeypatch, capsys, tmp_path) -> None:
    # An MP3 has no catalogue number field: the first pass shows one but never writes it.
    _write_mp3(tmp_path / "01.mp3", TRCK(encoding=3, text="1"))
    written: dict[str, Any] = {}

    _pass(monkeypatch, capsys, tmp_path, written, catno="CAT1")
    lines = _pass(monkeypatch, capsys, tmp_path, written, catno=None)

    assert [line for line in lines if line[1] == "catno"] == []


def test_a_tag_one_file_of_a_mixed_folder_took_is_cleared(monkeypatch, capsys, tmp_path) -> None:
    # The first file, an MP3, has no catalogue number field; the FLAC beside it took one.
    _write_mp3(tmp_path / "01.mp3", TRCK(encoding=3, text="1"))
    _flac(tmp_path, "02.flac", number="2")
    written: dict[str, Any] = {}

    _pass(monkeypatch, capsys, tmp_path, written, tracks=2, catno="CAT1")
    assert _catno(tmp_path / "02.flac") == ["CAT1"]
    _pass(monkeypatch, capsys, tmp_path, written, tracks=2, catno=None)

    assert _catno(tmp_path / "02.flac") is None


def test_the_rerun_loop_keeps_what_earlier_passes_wrote(monkeypatch, tmp_path) -> None:
    # salmon up: the catalogue number the first review gave is gone after the second, once the files are re-read.
    edit_metadata_stubs(monkeypatch)
    _flac(tmp_path)
    catnos = iter(["CAT1", None])

    async def review(metadata: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {**metadata, "catno": next(catnos)}

    monkeypatch.setattr(uploader, "review_metadata_with_ai", review)
    monkeypatch.setattr(uploader, "tag_files", tag_files)
    monkeypatch.setattr(uploader, "gather_tags", lambda path: _gather(tmp_path))
    site = partial(
        uploader.edit_metadata,
        str(tmp_path),
        _gather(tmp_path),
        _release(),
        None,
        "WEB",
        {},
        False,
        True,  # auto_rename: no retag question
        None,
        skip_integrity_check=True,
    )

    # Re-run the metadata step, then upload.
    ask(monkeypatch, site, [False, True])

    assert _catno(tmp_path / "01.flac") is None
