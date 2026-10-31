"""A metadata re-run removes the album tags the last retagging pass wrote and the new metadata no longer has (#835)."""

from copy import deepcopy
from functools import partial
from typing import Any

from mutagen.id3 import COMM, TALB, TCON, TDRC, TPUB, TXXX, TYER
from mutagen.mp4 import MP4
from test_interaction_typed_sites import ask, edit_metadata_stubs  # pyright: ignore[reportMissingImports]
from test_tagger_multi_value_tags import (  # pyright: ignore[reportMissingImports]
    _flac_album,
    _flac_tags,
    _formatting,  # noqa: F401 (an autouse fixture)
    _metadata,
    _mp3_album,
    _mp3_frames,
    _trackmeta,
    _write_m4a,
)

import salmon.uploader as uploader
from salmon.tagger.retagger import print_changes, tag_files
from salmon.tagger.tagfile import TagFile

ARTISTS = [("Dree Low", "main")]


def _release(tracks: int = 1, **fields: Any) -> dict[str, Any]:
    metadata = _metadata(*(_trackmeta(ARTISTS, f"Song {n}", str(n)) for n in range(1, tracks + 1)), genres=())
    metadata.update({"scene": False, "artists": ARTISTS, **fields})
    return metadata


def _rerun(monkeypatch, tmp_path, file, passes: list[dict[str, Any]], answers: list[Any]) -> None:
    """edit_metadata over the folder, its i-th review giving passes[i], with the real tag_files and its answers."""
    edit_metadata_stubs(monkeypatch)
    queue = [deepcopy(p) for p in passes]

    async def review(metadata: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return queue.pop(0)

    def gather_all(_path: str = "") -> dict[str, TagFile]:
        return {f.name: TagFile(str(f)) for f in sorted(tmp_path.iterdir())}

    async def gather(_path: str) -> dict[str, TagFile]:
        return gather_all()

    monkeypatch.setattr(uploader, "review_metadata_with_ai", review)
    monkeypatch.setattr(uploader, "check_tags", gather)
    monkeypatch.setattr(uploader, "gather_tags", gather_all)
    monkeypatch.setattr(uploader, "tag_files", tag_files)
    site = partial(
        uploader.edit_metadata,
        str(tmp_path),
        gather_all(),
        passes[0],
        None,
        "WEB",
        {},
        False,
        False,
        None,
        skip_integrity_check=True,
    )
    ask(monkeypatch, site, answers)


def test_flac_rerun_removes_the_catno_the_first_pass_wrote(monkeypatch, tmp_path) -> None:
    file = _flac_album(tmp_path)
    first = _release(catno="CAT1", label="Label")
    second = _release(label="Label")

    # Retag and re-run, retag and upload
    _rerun(monkeypatch, tmp_path, file, [first, second], [True, False, True, True])

    names = {name.upper() for name, _ in _flac_tags(file)}
    assert "CATALOGNUMBER" not in names
    assert "LABEL" in names


def test_mp3_rerun_removes_the_label_and_genres_the_first_pass_wrote(monkeypatch, tmp_path) -> None:
    file = _mp3_album(tmp_path)
    first = _release(label="Label", genres={"Rap"})
    second = _release()

    _rerun(monkeypatch, tmp_path, file, [first, second], [True, False, True, True])

    keys = [key for key, _ in _mp3_frames(file)]
    assert "TPUB" not in keys
    assert "TCON" not in keys
    assert "TALB" in keys


def test_a_tag_no_pass_wrote_survives_a_rerun(monkeypatch, tmp_path) -> None:
    file = _flac_album(tmp_path, UPC="123")
    first = _release(catno="CAT1")
    second = _release()

    _rerun(monkeypatch, tmp_path, file, [first, second], [True, False, True, True])

    names = {name.upper() for name, _ in _flac_tags(file)}
    assert "UPC" in names
    assert "CATALOGNUMBER" not in names


def test_the_first_pass_removes_nothing(monkeypatch, tmp_path) -> None:
    file = _flac_album(tmp_path, CATALOGNUMBER="OLD", LABEL="Old")

    _rerun(monkeypatch, tmp_path, file, [_release()], [True, True])

    names = {name.upper() for name, _ in _flac_tags(file)}
    assert {"CATALOGNUMBER", "LABEL"} <= names


def test_a_declined_pass_leaves_the_record_of_the_pass_before(monkeypatch, tmp_path) -> None:
    file = _flac_album(tmp_path)
    first = _release(catno="CAT1")
    second = _release(catno="CAT1")
    third = _release()

    # Pass 1 retags; pass 2 declines the retag; pass 3 clears the catno
    _rerun(monkeypatch, tmp_path, file, [first, second, third], [True, False, False, False, True, True])

    assert "CATALOGNUMBER" not in {name.upper() for name, _ in _flac_tags(file)}


def test_remove_on_flac_deletes_the_comment_in_any_case(tmp_path) -> None:
    file = _flac_album(tmp_path, catalognumber="X", Label="L")
    tagfile = TagFile(str(file))

    tagfile.remove("catno")
    tagfile.remove("label")
    tagfile.remove("upc")  # not there
    tagfile.save()

    assert {name.upper() for name, _ in _flac_tags(file)} == {"TRACKNUMBER", "DISCNUMBER", "TITLE"}


def test_remove_on_mp3_deletes_frames_including_txxx_in_another_case_and_both_date_frames(tmp_path) -> None:
    file = _mp3_album(
        tmp_path,
        TALB(text="A"),
        TPUB(text="L"),
        TDRC(text="2020"),
        TYER(text="2020"),
        TXXX(encoding=3, desc="artists", text="x"),
        COMM(encoding=3, lang="eng", desc="", text="c"),
        TCON(text="Rap"),
    )
    tagfile = TagFile(str(file))

    for key in ("label", "date", "artists", "comment", "catno"):  # catno has no MP3 mapping
        tagfile.remove(key)
    tagfile.save()

    assert [key for key, _ in _mp3_frames(file)] == ["TALB", "TCON", "TRCK"]


def test_remove_on_m4a_deletes_the_atom(tmp_path) -> None:
    file = tmp_path / "01.m4a"
    _write_m4a(file, **{"\xa9alb": ["A"], "\xa9gen": ["Rap"]})
    tagfile = TagFile(str(file))

    tagfile.remove("genre")
    tagfile.remove("label")  # no M4A mapping
    tagfile.remove("upc")
    tagfile.save()

    tags = MP4(file)
    assert "\xa9gen" not in tags
    assert tags["\xa9alb"] == ["A"]


def test_remove_on_a_file_without_tags_does_nothing(tmp_path) -> None:
    file = tmp_path / "x.txt"
    file.write_text("not audio")
    TagFile(str(file)).remove("label")


def test_print_changes_shows_a_cleared_field_as_removed(tmp_path, capsys) -> None:
    file = _flac_album(tmp_path, CATALOGNUMBER="CAT1", LABEL="L")
    tagfile = TagFile(str(file))

    print_changes({}, {}, tagfile, clear={"catno": "CAT1", "upc": "0123"})

    out = capsys.readouterr().out
    assert "CAT1" in out
    assert "(removed)" in out
    assert "upc" in out  # shown with the value the last pass wrote, whatever the file reads back


def test_mp3_rerun_shows_the_comment_it_removes(monkeypatch, tmp_path, capsys) -> None:
    file = _mp3_album(tmp_path)
    first = _release(comment="Ripped by me")
    second = _release()

    _rerun(monkeypatch, tmp_path, file, [first, second], [True, False, True, True])

    out = capsys.readouterr().out
    assert any("comment" in line and "Ripped by me" in line and "(removed)" in line for line in out.splitlines())
    assert "COMM" not in [key.split(":")[0] for key, _ in _mp3_frames(file)]


def test_a_flac_after_an_mp3_loses_its_catno_and_the_preview_shows_it(monkeypatch, tmp_path, capsys) -> None:
    from mutagen.id3 import TRCK
    from test_tagger_multi_value_tags import _write_flac, _write_mp3  # pyright: ignore[reportMissingImports]

    _write_mp3(tmp_path / "01.mp3", TRCK(text="1"))
    _write_flac(tmp_path / "02.flac", TRACKNUMBER="2", DISCNUMBER="1", TITLE="Song 2")
    first = _release(2, catno="CAT1")
    second = _release(2)

    _rerun(monkeypatch, tmp_path, tmp_path / "01.mp3", [first, second], [True, False, True, True])

    out = capsys.readouterr().out
    assert any("catno" in line and "CAT1" in line and "(removed)" in line for line in out.splitlines())
    assert "CATALOGNUMBER" not in {name.upper() for name, _ in _flac_tags(tmp_path / "02.flac")}


def test_a_catno_no_file_has_a_field_for_is_not_listed_as_removed(monkeypatch, tmp_path, capsys) -> None:
    file = _mp3_album(tmp_path)
    first = _release(catno="CAT1")
    second = _release()

    _rerun(monkeypatch, tmp_path, file, [first, second], [True, False, True, True])

    out = capsys.readouterr().out
    assert "(removed)" not in out
