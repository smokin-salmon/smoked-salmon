"""Playlists and cue sheets are rewritten to name the files salmon renamed, in the encoding they were written in."""

import os
import shutil
from pathlib import Path

import anyio
import pytest
from test_tagger_retagger import _release, _single_folder

from salmon.converter import conversions
from salmon.errors import NoncompliantFolderStructure, UploadError
from salmon.tagger import folderstructure, renames
from salmon.tagger.playlists import rewrite_playlists
from salmon.tagger.retagger import rename_files


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
    captured = capsys.readouterr()
    assert "cp1252 cannot hold" in captured.out
    # Only ASCII names were renamed, so its "café", naming no file, is no sign of another code page.
    assert "may not be its encoding" not in captured.out


@pytest.mark.parametrize(("code_page", "name"), [("cp1251", "Привет"), ("shift_jis", "日本")])
def test_a_cue_in_another_code_page_is_warned_about_where_its_names_did_not_follow(
    tmp_path, capsys, code_page: str, name: str
) -> None:
    """EAC writes cue sheets in the system's code page, which reads as cp1252 without error but names nothing."""
    _tracks(tmp_path, f"01 {name}.flac", "02 b.flac")
    cue = tmp_path / "a.cue"
    cue.write_bytes(f'FILE "01 {name}.flac" WAVE\r\nFILE "02 b.flac" WAVE\r\n'.encode(code_page))

    rewrite_playlists(
        str(tmp_path), _rename(tmp_path, (f"01 {name}.flac", f"01. {name}.flac"), ("02 b.flac", "02. B.flac"))
    )

    assert cue.read_bytes() == f'FILE "01 {name}.flac" WAVE\r\nFILE "02. B.flac" WAVE\r\n'.encode(code_page)
    out = capsys.readouterr().out

    assert "a.cue: read as cp1252, which may not be its encoding" in out


def test_a_cue_in_cp1250_is_warned_about_where_a_folder_rename_moved_a_renamed_file(tmp_path, capsys) -> None:
    """The forbidden-character rename renames the file, then its folder: the entry is moved, naming no file."""
    _tracks(tmp_path, "AC?DC/01 ő?.flac")
    cue = tmp_path / "a.cue"
    cue.write_bytes('FILE "AC?DC\\01 ő?.flac" WAVE\r\n'.encode("cp1250"))

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("AC?DC/01 ő?.flac", "AC?DC/01 ő_.flac"), ("AC?DC", "AC_DC")))

    assert cue.read_bytes() == 'FILE "AC_DC\\01 ő?.flac" WAVE\r\n'.encode("cp1250")
    assert "a.cue: read as cp1252, which may not be its encoding: check AC?DC\\01 õ?.flac" in capsys.readouterr().out


def test_as_before_a_cue_is_not_warned_about_where_a_folder_rename_moved_a_file_that_is_there(tmp_path, capsys) -> None:
    _tracks(tmp_path, "AC?DC/01 é.flac", "Ö/02.flac")
    cue = tmp_path / "a.cue"
    cue.write_bytes('FILE "AC?DC\\01 é.flac" WAVE\r\n'.encode("cp1252"))

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("AC?DC", "AC_DC"), ("Ö/02.flac", "Ö/02_.flac")))

    assert cue.read_bytes() == 'FILE "AC_DC\\01 é.flac" WAVE\r\n'.encode("cp1252")
    assert capsys.readouterr().out == ""


def test_as_before_a_cue_is_not_warned_about_where_a_folder_rename_moved_a_bare_entry_that_gained_a_space(
    tmp_path, capsys
) -> None:
    """A bare entry renamed to a path with a space is quoted; the existence check uses the unquoted path."""
    _tracks(tmp_path, "Café/01 é.flac")
    cue = tmp_path / "a.cue"
    cue.write_bytes("FILE Café\\01 é.flac WAVE\r\n".encode("cp1252"))

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("Café", "Café Album")))

    assert cue.read_bytes() == 'FILE "Café Album\\01 é.flac" WAVE\r\n'.encode("cp1252")
    assert capsys.readouterr().out == ""


def test_as_before_a_true_cp1252_cue_is_not_warned_about(tmp_path, capsys) -> None:
    """An untouched name that is there, and a name that was never there, read right as cp1252: nothing to check."""
    _tracks(tmp_path, "01 Café.flac", "02 Où.flac")
    cue = tmp_path / "a.cue"
    cue.write_bytes('FILE "01 Café.flac" WAVE\r\nFILE "02 Où.flac" WAVE\r\nFILE "03.flac" WAVE\r\n'.encode("cp1252"))

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01 Café.flac", "01. Été.flac")))

    assert cue.read_bytes() == (
        'FILE "01. Été.flac" WAVE\r\nFILE "02 Où.flac" WAVE\r\nFILE "03.flac" WAVE\r\n'.encode("cp1252")
    )
    out = capsys.readouterr().out

    assert out == ""


def test_a_true_cp1252_cue_in_a_non_ascii_album_folder_is_not_warned_about(tmp_path, capsys) -> None:
    """Only ASCII names were renamed: the album folder's own name is no sign of another code page."""
    album = tmp_path / "Björk - Homogenic"
    _tracks(album, "01.flac")
    cue = album / "a.cue"
    cue.write_bytes('FILE "01.flac" WAVE\r\nFILE "03 Café.flac" WAVE\r\n'.encode("cp1252"))

    rewrite_playlists(str(album), _rename(album, ("01.flac", "01. One.flac")))

    assert cue.read_bytes() == 'FILE "01. One.flac" WAVE\r\nFILE "03 Café.flac" WAVE\r\n'.encode("cp1252")
    assert capsys.readouterr().out == ""


def test_as_before_a_utf8_cue_is_not_warned_about(tmp_path, capsys) -> None:
    """Read as UTF-8, a name that is not there is not misread, whatever was renamed."""
    _tracks(tmp_path, "01 Café.flac")
    cue = tmp_path / "a.cue"
    cue.write_bytes('FILE "01 Café.flac" WAVE\r\nFILE "03 Où.flac" WAVE\r\n'.encode())

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01 Café.flac", "01. Été.flac")))

    assert cue.read_bytes() == 'FILE "01. Été.flac" WAVE\r\nFILE "03 Où.flac" WAVE\r\n'.encode()
    assert capsys.readouterr().out == ""


# The two places that rename


def _rename_files(album: Path, tags: dict, monkeypatch) -> None:
    """The metadata review's rename of the tracks, each named for its track number."""
    monkeypatch.setattr(
        "salmon.tagger.retagger.generate_file_name", lambda tags, ext, _multi, **_: f"{tags['tracknumber']}.01 One{ext}"
    )
    metadata = {"tracks": {"1": {"1": {"artists": [("A", "main")]}}}}
    anyio.run(lambda: rename_files(str(album), tags, metadata, auto_rename=True, spectral_ids=None, source="WEB"))


def test_the_track_renames_of_the_metadata_review_rewrite_the_playlists(tmp_path, monkeypatch) -> None:
    _tracks(tmp_path, "01.flac")
    playlist = tmp_path / "a.m3u"
    playlist.write_text("01.flac\n")

    _rename_files(tmp_path, {"01.flac": {"tracknumber": "1", "title": "One"}}, monkeypatch)

    assert playlist.read_text() == "1.01 One.flac\n"


def test_a_playlist_moved_out_of_a_disc_folder_names_the_tracks_from_its_new_folder(tmp_path, monkeypatch) -> None:
    _tracks(tmp_path, "CD1/01.flac")
    (tmp_path / "CD1" / "rip.cue").write_text('FILE "01.flac" WAVE\n')
    (tmp_path / "CD1" / "rip.m3u").write_text("01.flac\n")

    _rename_files(tmp_path, {"CD1/01.flac": {"tracknumber": "1", "title": "One"}}, monkeypatch)

    assert (tmp_path / "1.01 One.flac").is_file()
    assert not (tmp_path / "CD1").exists()
    assert (tmp_path / "rip.cue").read_text() == 'FILE "1.01 One.flac" WAVE\n'
    assert (tmp_path / "rip.m3u").read_text() == "1.01 One.flac\n"


def test_a_playlist_moved_with_its_tracks_also_rebases_entries_for_files_that_stayed(tmp_path, monkeypatch) -> None:
    _tracks(tmp_path, "CD1/01.flac", "Bonus/bonus.flac")
    (tmp_path / "CD1" / "rip.m3u").write_text("01.flac\n../Bonus/bonus.flac\n")

    _rename_files(tmp_path, {"CD1/01.flac": {"tracknumber": "1", "title": "One"}}, monkeypatch)

    assert (tmp_path / "rip.m3u").read_text() == "1.01 One.flac\nBonus/bonus.flac\n"


def test_a_move_of_other_files_that_fails_still_updates_the_playlists_for_what_moved(tmp_path, monkeypatch) -> None:
    _tracks(tmp_path, "CD1/01.flac")
    (tmp_path / "CD1" / "a.cue").write_text('FILE "01.flac" WAVE\n')
    (tmp_path / "CD1" / "b.cue").write_text('FILE "01.flac" WAVE\n')
    real_move = shutil.move

    def move(src, dst):
        if str(src).endswith("b.cue"):
            raise OSError(5, "I/O error")
        return real_move(src, dst)

    monkeypatch.setattr("salmon.tagger.retagger.shutil.move", move)

    with pytest.raises(OSError, match="I/O error"):
        _rename_files(tmp_path, {"CD1/01.flac": {"tracknumber": "1", "title": "One"}}, monkeypatch)

    assert (tmp_path / "a.cue").read_text() == 'FILE "1.01 One.flac" WAVE\n'
    assert (tmp_path / "CD1" / "b.cue").read_text() == 'FILE "../1.01 One.flac" WAVE\n'


def test_a_batch_put_back_leaves_the_playlists_as_they_were(tmp_path, monkeypatch) -> None:
    _tracks(tmp_path, "CD1/01.flac")
    playlist = tmp_path / "CD1" / "rip.m3u"
    playlist.write_text("01.flac\n")

    def full_disk(*_args, **_kwargs) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(conversions, "note_renamed", full_disk)

    with pytest.raises(UploadError):
        _rename_files(tmp_path, {"CD1/01.flac": {"tracknumber": "1", "title": "One"}}, monkeypatch)

    assert playlist.read_text() == "01.flac\n"
    assert (tmp_path / "CD1" / "01.flac").is_file()


def test_the_folder_structure_check_rewrites_the_playlists_of_what_it_renames(tmp_path) -> None:
    album = tmp_path / "Album"
    _tracks(album, "x" * 60 + ".flac")
    playlist = album / "a.m3u"
    playlist.write_text("x" * 60 + ".flac\n")

    folderstructure._check_path_lengths(str(album), False, len("Album") + 1 + 40)

    (renamed,) = [p.name for p in album.glob("*.flac")]
    assert renamed != "x" * 60 + ".flac"
    assert playlist.read_text() == renamed + "\n"


def test_a_bare_cue_name_that_gains_a_space_is_quoted(tmp_path) -> None:
    _tracks(tmp_path, "01.flac")
    cue = tmp_path / "a.cue"
    cue.write_text("FILE 01.flac WAVE\n")

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("01.flac", "01. One.flac")))

    assert cue.read_text() == 'FILE "01. One.flac" WAVE\n'


def test_an_untouched_entry_keeps_its_unicode_form_when_its_folder_is_renamed(tmp_path) -> None:
    _tracks(tmp_path, "CD1/Cafe\u0301.flac")
    playlist = tmp_path / "a.m3u8"
    playlist.write_text("CD1/Cafe\u0301.flac\n", encoding="utf-8")

    rewrite_playlists(str(tmp_path), _rename(tmp_path, ("CD1", "Disc 1")))

    assert playlist.read_text(encoding="utf-8") == "Disc 1/Cafe\u0301.flac\n"


def test_a_failed_write_leaves_no_temporary_file(tmp_path, monkeypatch, capsys) -> None:
    _tracks(tmp_path, "01.flac")
    playlist = tmp_path / "a.m3u"
    playlist.write_text("01.flac\n")
    renames = _rename(tmp_path, ("01.flac", "1.flac"))

    def refuse(*_args) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("salmon.tagger.playlists.os.replace", refuse)
    rewrite_playlists(str(tmp_path), renames)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["1.flac", "a.m3u"]
    assert playlist.read_text() == "01.flac\n"
    captured = capsys.readouterr()
    assert "Could not write" in captured.out


def test_the_playlists_of_a_multi_disc_release_kept_in_one_folder_follow_their_discs(tmp_path, monkeypatch) -> None:
    _single_folder(monkeypatch)
    tags, metadata = _release(tmp_path, {"CD1/01.flac": (1, 1), "CD1/02.flac": (1, 2), "CD2/01.flac": (2, 1)})
    (tmp_path / "CD1" / "rip.cue").write_bytes(b"FILE 01.flac WAVE\r\nFILE 02.flac WAVE\r\n")
    (tmp_path / "CD1" / "rip.m3u").write_bytes(b"01.flac\r\n02.flac\r\n")
    (tmp_path / "CD2" / "rip.cue").write_bytes(b'FILE "01.flac" WAVE\r\n')
    (tmp_path / "CD2" / "rip.m3u8").write_bytes(b"../CD1/02.flac\r\n01.flac\r\n")

    anyio.run(lambda: rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD"))

    names = sorted(p.name for p in tmp_path.glob("*.flac"))
    assert len(names) == 3
    first, second, third = names
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix != ".flac") == [
        "rip.1.cue",
        "rip.1.m3u",
        "rip.2.cue",
        "rip.2.m3u8",
    ]
    assert (tmp_path / "rip.1.cue").read_bytes() == f'FILE "{first}" WAVE\r\nFILE "{second}" WAVE\r\n'.encode()
    assert (tmp_path / "rip.1.m3u").read_bytes() == f"{first}\r\n{second}\r\n".encode()
    assert (tmp_path / "rip.2.cue").read_bytes() == f'FILE "{third}" WAVE\r\n'.encode()
    assert (tmp_path / "rip.2.m3u8").read_bytes() == f"{second}\r\n{third}\r\n".encode()


def test_a_stopped_folder_check_batch_leaves_the_playlist_naming_what_was_renamed(
    tmp_path, monkeypatch, capsys
) -> None:
    album = tmp_path / "Album"
    one, two = "a" * 60 + ".flac", "b" * 60 + ".flac"
    _tracks(album, one, two)
    playlist = album / "a.m3u"
    playlist.write_text(f"{one}\n{two}\n")
    real = renames._rename_no_clobber
    calls = []

    def rename_once(old: str, new: str) -> None:
        calls.append(old)
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied", old)
        real(old, new)

    monkeypatch.setattr(renames, "_rename_no_clobber", rename_once)

    with pytest.raises(NoncompliantFolderStructure):
        folderstructure._check_path_lengths(str(album), False, len("Album") + 1 + 40)

    (renamed,) = [p.name for p in album.glob("*.flac") if p.name not in (one, two)]
    assert (album / two).is_file()
    assert playlist.read_text() == f"{renamed}\n{two}\n"
    captured = capsys.readouterr()
    assert "Renaming" in captured.out


def test_an_absolute_entry_outside_the_album_stays_absolute_when_its_playlist_moves(tmp_path, monkeypatch) -> None:
    elsewhere = tmp_path / "elsewhere.flac"
    elsewhere.write_bytes(b"fLaC")
    album = tmp_path / "Album"
    _tracks(album, "CD1/01.flac")
    (album / "CD1" / "rip.m3u").write_text(f"01.flac\n{elsewhere}\n")

    _rename_files(album, {"CD1/01.flac": {"tracknumber": "1", "title": "One"}}, monkeypatch)

    assert (album / "rip.m3u").read_text() == f"1.01 One.flac\n{elsewhere}\n"
