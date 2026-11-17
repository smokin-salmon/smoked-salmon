"""Playlists and cue sheets are rewritten to name the files salmon renamed, in the encoding they were written in."""

import os
from pathlib import Path

import pytest

from salmon.converter import conversions
from salmon.errors import UploadError
from salmon.tagger import folderstructure
from salmon.tagger.playlists import rewrite_playlists
from salmon.tagger.retagger import _rename_or_undo


def _tracks(album: Path, *names: str) -> None:
    for name in names:
        target = album / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fLaC")


def _rename(album: Path, *pairs: tuple[str, str]) -> list[tuple[str, str]]:
    """Rename on disk as salmon does, and give the (old, new) absolute paths."""
    renames = [(str(album / old), str(album / new)) for old, new in pairs]
    for old, new in renames:
        os.makedirs(os.path.dirname(new), exist_ok=True)
        os.rename(old, new)
    return renames


def test_an_m3u_names_the_new_files_and_keeps_its_extinf_lines(tmp_path) -> None:
    _tracks(tmp_path, "01.flac", "02.flac")
    playlist = tmp_path / "a.m3u"
    playlist.write_bytes(b"#EXTM3U\n#EXTINF:100,A - One\n01.flac\n#EXTINF:90,A - Two\n02.flac\n")

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01.flac", "01. One.flac"), ("02.flac", "02. Two.flac")))

    assert playlist.read_bytes() == (b"#EXTM3U\n#EXTINF:100,A - One\n01. One.flac\n#EXTINF:90,A - Two\n02. Two.flac\n")


def test_an_m3u8_keeps_its_bom_and_non_ascii_names(tmp_path) -> None:
    _tracks(tmp_path, "01 Café.flac")
    playlist = tmp_path / "a.M3U8"
    playlist.write_bytes(b"\xef\xbb\xbf#EXTM3U\n" + "01 Café.flac\n".encode())

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01 Café.flac", "01. Été.flac")))

    assert playlist.read_bytes() == b"\xef\xbb\xbf#EXTM3U\n" + "01. Été.flac\n".encode()


def test_an_entry_in_another_unicode_form_is_matched(tmp_path) -> None:
    _tracks(tmp_path, "01 Café.flac")
    playlist = tmp_path / "a.m3u8"
    playlist.write_text("01 Café.flac\n", encoding="utf-8")

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01 Café.flac", "01.flac")))

    assert playlist.read_text(encoding="utf-8") == "01.flac\n"


def test_a_cp1252_m3u_is_written_back_in_cp1252(tmp_path) -> None:
    _tracks(tmp_path, "01 Café.flac")
    playlist = tmp_path / "a.m3u"
    playlist.write_bytes("01 Café.flac\r\n".encode("cp1252"))

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01 Café.flac", "01. Été.flac")))

    assert playlist.read_bytes() == "01. Été.flac\r\n".encode("cp1252")


def test_a_cue_sheet_names_each_file_quoted_or_not(tmp_path) -> None:
    _tracks(tmp_path, "01.flac", "02 b.flac", "03.flac")
    cue = tmp_path / "a.cue"
    cue.write_bytes(
        b'REM x\r\nFILE "01.flac" WAVE\r\n  TRACK 01 AUDIO\r\nFILE "02 b.flac" WAVE\r\nFILE 03.flac WAVE\r\n'
    )

    rewrite_playlists(
        str(tmp_path), _rename(tmp_path, ("01.flac", "1.flac"), ("02 b.flac", "2 B.flac"), ("03.flac", "3.flac"))
    )

    assert cue.read_bytes() == (
        b'REM x\r\nFILE "1.flac" WAVE\r\n  TRACK 01 AUDIO\r\nFILE "2 B.flac" WAVE\r\nFILE 3.flac WAVE\r\n'
    )


def test_a_move_out_of_a_disc_folder_is_followed_in_the_entry_s_separators(tmp_path) -> None:
    _tracks(tmp_path, "CD1/01.flac", "CD2/01.flac")
    backslash = tmp_path / "back.m3u"
    backslash.write_bytes(b"CD1\\01.flac\r\nCD2\\01.flac\r\n")
    slash = tmp_path / "slash.m3u"
    slash.write_bytes(b"./CD1/01.flac\n")

    renames = _rename(tmp_path, ("CD1/01.flac", "1.01 One.flac"), ("CD2/01.flac", "2.01 Two.flac"))
    rewrite_playlists(str(tmp_path), renames)

    assert backslash.read_bytes() == b"1.01 One.flac\r\n2.01 Two.flac\r\n"
    assert slash.read_bytes() == b"1.01 One.flac\n"


def test_a_playlist_in_a_sub_folder_and_one_that_moved_with_it_are_followed(tmp_path) -> None:
    _tracks(tmp_path, "CD1/01.flac")
    (tmp_path / "CD1" / "a.m3u").write_text("01.flac\n")
    (tmp_path / "CD1" / "._a.m3u").write_text("01.flac\n")

    renames = _rename(tmp_path, ("CD1/01.flac", "CD1/1.flac"), ("CD1", "Disc 1"))
    rewrite_playlists(str(tmp_path), renames)

    assert (tmp_path / "Disc 1" / "a.m3u").read_text() == "1.flac\n"
    assert (tmp_path / "Disc 1" / "._a.m3u").read_text() == "01.flac\n"


def test_a_url_and_a_comment_are_left_alone(tmp_path) -> None:
    _tracks(tmp_path, "01.flac")
    playlist = tmp_path / "a.m3u"
    text = "# 01.flac\nhttp://example.com/01.flac\n"
    playlist.write_text(text)

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01.flac", "1.flac")))

    assert playlist.read_text() == text


def test_an_entry_whose_file_is_gone_is_warned_about_and_left(tmp_path, capsys) -> None:
    _tracks(tmp_path, "01.flac")
    playlist = tmp_path / "a.m3u"
    # In another case: a file that existed on a volume that ignores it, and matches no rename on this one.
    playlist.write_text("01.FLAC\nnever existed.flac\n")

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01.flac", "1.flac")))

    assert playlist.read_text() == "01.FLAC\nnever existed.flac\n"
    out = capsys.readouterr().out
    assert "a.m3u" in out
    assert "01.FLAC" in out
    assert "never existed" not in out


def test_a_name_cp1252_cannot_hold_is_left_and_warned_about(tmp_path, capsys) -> None:
    _tracks(tmp_path, "01.flac", "02.flac")
    playlist = tmp_path / "a.m3u"
    playlist.write_bytes("01.flac\n02.flac\n".encode("cp1252") + "caf\xe9\n".encode("cp1252"))

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01.flac", "01 東.flac"), ("02.flac", "2.flac")))

    assert playlist.read_bytes() == "01.flac\n2.flac\ncaf\xe9\n".encode("cp1252")
    assert "cp1252 cannot hold" in capsys.readouterr().out


# The two places that rename


def test_the_track_renames_of_the_metadata_review_rewrite_the_playlists(tmp_path) -> None:
    _tracks(tmp_path, "01.flac")
    playlist = tmp_path / "a.m3u"
    playlist.write_text("01.flac\n")

    _rename_or_undo(str(tmp_path), [(str(tmp_path / "01.flac"), str(tmp_path / "01. One.flac"))])

    assert playlist.read_text() == "01. One.flac\n"


def test_a_batch_put_back_leaves_the_playlists_as_they_were(tmp_path, monkeypatch) -> None:
    _tracks(tmp_path, "01.flac")
    playlist = tmp_path / "a.m3u"
    playlist.write_text("01.flac\n")

    def full_disk(*_args, **_kwargs) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(conversions, "note_renamed", full_disk)

    with pytest.raises(UploadError):
        _rename_or_undo(str(tmp_path), [(str(tmp_path / "01.flac"), str(tmp_path / "01. One.flac"))])

    assert playlist.read_text() == "01.flac\n"
    assert (tmp_path / "01.flac").is_file()


def test_the_folder_structure_check_rewrites_the_playlists_of_what_it_renames(tmp_path) -> None:
    album = tmp_path / "Album"
    _tracks(album, "x" * 60 + ".flac")
    playlist = album / "a.m3u"
    playlist.write_text("x" * 60 + ".flac\n")

    folderstructure._check_path_lengths(str(album), False, len("Album") + 1 + 40)

    (renamed,) = [p.name for p in album.glob("*.flac")]
    assert renamed != "x" * 60 + ".flac"
    assert playlist.read_text() == renamed + "\n"
