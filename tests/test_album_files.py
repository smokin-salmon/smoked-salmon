"""album_files: an album's files of some kinds, found once for every walk that lists them (#904 item 4).

Each walk it replaced kept what it found; "as before" tests pin that. The changes are declared: a path ending in a
separator keeps its folder names whole, a file named only ".flac" counts as a FLAC, and the one MQA check of
upload.mqa_check_all_tracks = false reads the first track rather than the first name.

A folder it can't read stops it with an UploadError naming the folder, as #958's walk in salmon up does: the check
commands and the converters then stop rather than pass on the files they could list. check all asks it to skip one, as
its Folders row names it.
"""

import importlib
import os
from functools import partial
from pathlib import Path

import anyio
import pytest
from asyncclick.testing import CliRunner
from helpers.checks_all_command import _fail_scandir

import salmon.checks
import salmon.checks.upconverts as upconverts
import salmon.common.files as files
import salmon.uploader.preparation as preparation
from salmon.checks import check, mqa_test
from salmon.checks import logs as logs_module
from salmon.common.constants import AUDIO_EXTENSIONS
from salmon.common.files import get_audio_files
from salmon.converter.downconverting import convert_folder
from salmon.converter.transcoding import transcode_folder
from salmon.errors import UploadError
from salmon.prepared import Prepared

# The check commands named integrity and mqa hide these modules as attributes of salmon.checks.
integrity = importlib.import_module("salmon.checks.integrity")
mqa = importlib.import_module("salmon.checks.mqa")


def _album(root: Path, *names: str) -> Path:
    """A real folder holding these (empty) files."""
    for name in names:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    return root


def _run(*args: str):
    return anyio.run(partial(CliRunner().invoke, check, list(args)))


def _spy(monkeypatch, module, name: str, returns) -> list[str]:
    """Replace an async check of one file by one that records the path it is given."""
    seen: list[str] = []

    async def spy(path: str, *_args, **_kwargs):
        seen.append(path)
        return returns

    monkeypatch.setattr(module, name, spy)
    return seen


# album_files


def test_album_files_are_joined_to_the_path_in_track_order(tmp_path):
    album = _album(tmp_path, "Disc 10/1.flac", "Disc 2/10.flac", "Disc 2/2.flac", "cover.jpg")
    assert files.album_files(str(album), [".flac"]) == [
        str(album / "Disc 2" / "2.flac"),
        str(album / "Disc 2" / "10.flac"),
        str(album / "Disc 10" / "1.flac"),
    ]


def test_album_files_match_the_end_of_the_name_in_any_case(tmp_path):
    album = _album(tmp_path, "01.FLAC", "02.Mp3", "03.m4a", "04.flac.txt", "rip.log", "flac")
    assert [os.path.basename(p) for p in files.album_files(str(album), AUDIO_EXTENSIONS)] == [
        "01.FLAC",
        "02.Mp3",
        "03.m4a",
    ]
    assert [os.path.basename(p) for p in files.album_files(str(album), (".log",))] == ["rip.log"]


def test_album_files_of_a_missing_folder_are_none(tmp_path):
    assert files.album_files(str(tmp_path / "gone"), AUDIO_EXTENSIONS) == []


# get_audio_files, on album_files


def test_a_path_ending_in_a_separator_keeps_the_folder_names_whole(tmp_path):
    album = _album(tmp_path, "Disc 1/01.flac", "Disc 2/01.flac")
    assert get_audio_files(str(album) + os.sep) == [
        os.path.join("Disc 1", "01.flac"),
        os.path.join("Disc 2", "01.flac"),
    ]


def test_a_file_named_only_dot_flac_is_listed_as_the_other_walks_list_it(tmp_path):
    album = _album(tmp_path, ".flac", "01.flac")
    assert get_audio_files(str(album)) == ["01.flac", ".flac"]


def test_as_before_audio_files_are_named_from_the_album_in_track_order(tmp_path):
    album = _album(tmp_path, "CD2/1.mp3", "CD1/10.m4a", "CD1/2.FLAC", "notes.txt")
    assert get_audio_files(str(album)) == [
        os.path.join("CD1", "2.FLAC"),
        os.path.join("CD1", "10.m4a"),
        os.path.join("CD2", "1.mp3"),
    ]


def test_as_before_the_log_check_finds_every_kind_of_audio(tmp_path):
    album = _album(tmp_path, "Disc 1/01.flac", "Disc 1/02.MP3", "Disc 2/01.m4a", "Disc 2/rip.log")
    assert sorted(logs_module._find_audio_files(str(album))) == sorted(
        [str(album / "Disc 1" / "01.flac"), str(album / "Disc 1" / "02.MP3"), str(album / "Disc 2" / "01.m4a")]
    )


# The MQA check's one track


def test_with_one_mqa_check_the_first_track_is_read_not_the_first_name(tmp_path, monkeypatch):
    album = _album(tmp_path, "2 Two.flac", "10 Ten.flac")
    seen = _spy(monkeypatch, mqa, "check_mqa", False)
    anyio.run(partial(mqa_test, str(album), all_files=False))
    assert [os.path.basename(p) for p in seen] == ["2 Two.flac"]


def test_the_upload_prepares_the_mqa_check_of_the_same_first_track(tmp_path):
    album = _album(tmp_path, "2 Two.flac", "10 Ten.flac", "cover.jpg")
    assert preparation._flacs(str(album)) == [str(album / "2 Two.flac"), str(album / "10 Ten.flac")]


def test_as_before_every_flac_is_checked_for_mqa_and_nothing_else(tmp_path, monkeypatch):
    album = _album(tmp_path, "Disc 1/01.flac", "Disc 2/01.FLAC", "02.mp3", "03.m4a")
    seen = _spy(monkeypatch, mqa, "check_mqa", False)
    anyio.run(mqa_test, str(album))
    assert sorted(seen) == sorted([str(album / "Disc 1" / "01.flac"), str(album / "Disc 2" / "01.FLAC")])


# The single check commands, on a folder


def test_as_before_check_mqa_reads_every_flac_and_mp3(tmp_path, monkeypatch):
    album = _album(tmp_path, "Disc 1/01.flac", "Disc 2/02.MP3", "03.m4a", "cover.jpg")
    seen = _spy(monkeypatch, mqa, "check_mqa", False)
    result = _run("mqa", str(album))
    assert result.exit_code == 0, result.output
    assert sorted(seen) == sorted([str(album / "Disc 1" / "01.flac"), str(album / "Disc 2" / "02.MP3")])
    assert result.output.count("Did not find MQA syncword") == 2


def test_as_before_check_log_scores_every_log(tmp_path, monkeypatch):
    album = _album(tmp_path, "Disc 1/rip.log", "Disc 2/RIP.LOG", "01.flac", "info.txt")
    seen = _spy(monkeypatch, salmon.checks, "check_log_cambia", None)
    result = _run("log", str(album))
    assert result.exit_code == 0, result.output
    assert sorted(seen) == sorted([str(album / "Disc 1" / "rip.log"), str(album / "Disc 2" / "RIP.LOG")])


def test_as_before_check_upconv_reads_every_flac(tmp_path, monkeypatch):
    album = _album(tmp_path, "Disc 1/01.flac", "Disc 2/01.FLAC", "02.mp3")
    seen = _spy(monkeypatch, upconverts, "_upconvert_check_handler", None)
    result = _run("upconv", str(album))
    assert result.exit_code == 0, result.output
    assert sorted(seen) == sorted([str(album / "Disc 1" / "01.flac"), str(album / "Disc 2" / "01.FLAC")])


def test_as_before_the_integrity_check_decodes_every_flac_and_mp3(tmp_path, monkeypatch):
    album = _album(tmp_path, "Disc 1/01.flac", "Disc 2/02.MP3", "03.m4a")
    passed = integrity.IntegrityResult(True, "", checked=1)
    flacs = _spy(monkeypatch, integrity, "_check_flac_integrity", passed)
    mp3s = _spy(monkeypatch, integrity, "_check_mp3_integrity", passed)
    result = anyio.run(integrity.check_integrity, str(album))
    assert (flacs, mp3s) == ([str(album / "Disc 1" / "01.flac")], [str(album / "Disc 2" / "02.MP3")])
    assert result.passed and result.checked == 2


def test_as_before_sanitizing_reencodes_every_flac_and_mp3(tmp_path, monkeypatch):
    album = _album(tmp_path, "Disc 1/01.flac", "Disc 2/02.MP3", "03.m4a")
    monkeypatch.setattr(integrity, "_save_embedded_cover", lambda *_: True)
    seen = _spy(monkeypatch, integrity, "_sanitize_file", True)
    assert anyio.run(integrity.sanitize_integrity, str(album)) is True
    assert sorted(seen) == sorted([str(album / "Disc 1" / "01.flac"), str(album / "Disc 2" / "02.MP3")])


def test_as_before_the_upload_prepares_crcs_when_a_disc_folder_holds_a_log(tmp_path, monkeypatch):
    album = _album(tmp_path / "album", "Disc 1/rip.LOG", "Disc 1/01.flac")
    seen = _spy(monkeypatch, logs_module, "_calculate_file_crc_async", 0)
    anyio.run(preparation.prepare_log_crcs, Prepared(str(album), str(tmp_path / "prepared")))
    assert seen == [str(album / "Disc 1" / "01.flac")]


def test_as_before_the_upload_prepares_no_crc_without_a_log(tmp_path, monkeypatch):
    album = _album(tmp_path / "album", "Disc 1/01.flac", "Disc 1/notes.txt")
    seen = _spy(monkeypatch, logs_module, "_calculate_file_crc_async", 0)
    anyio.run(preparation.prepare_log_crcs, Prepared(str(album), str(tmp_path / "prepared")))
    assert seen == []


# An unreadable folder: named, so nothing passes on the files it could not list


WHAT_TO_DO = "fix it or remove it from the album, then run salmon again."
NEEDS_PERMISSIONS = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs file permissions that bind (not root)"
)


@pytest.fixture
def discs(tmp_path) -> Path:
    """An album of two disc folders, a FLAC, an MP3 and a log in each."""
    return _album(
        tmp_path / "Album", *(f"Disc {n}/{name}" for n in (1, 2) for name in ("01.flac", "02.mp3", "rip.log"))
    )


def _could_not_read(album: Path, name: str, why: str = "Refused by the test") -> str:
    return f"Could not read {name} in {album} ({why}): {WHAT_TO_DO}"


def test_an_unreadable_folder_stops_the_listing_and_is_named_from_the_album(discs, monkeypatch):
    (discs / "Disc 2" / "Scans").mkdir()
    _fail_scandir(monkeypatch, PermissionError, "Scans")
    with pytest.raises(UploadError) as error:
        files.album_files(str(discs), AUDIO_EXTENSIONS)
    assert str(error.value) == _could_not_read(discs, os.path.join("Disc 2", "Scans"))
    assert isinstance(error.value.__cause__, PermissionError)


def test_get_audio_files_stops_on_an_unreadable_folder(discs, monkeypatch):
    _fail_scandir(monkeypatch, OSError, "Disc 2")
    with pytest.raises(UploadError, match="Could not read Disc 2 in "):
        get_audio_files(str(discs))


@NEEDS_PERMISSIONS
def test_a_disc_folder_without_permissions_is_named(discs):
    os.chmod(discs / "Disc 2", 0)
    try:
        with pytest.raises(UploadError) as error:
            get_audio_files(str(discs))
    finally:
        os.chmod(discs / "Disc 2", 0o755)
    assert str(error.value) == _could_not_read(discs, "Disc 2", "Permission denied")


@NEEDS_PERMISSIONS
def test_an_album_folder_that_cannot_be_read_is_named_by_its_path(discs):
    os.chmod(discs, 0)
    try:
        with pytest.raises(UploadError) as error:
            get_audio_files(str(discs))
    finally:
        os.chmod(discs, 0o755)
    assert str(error.value) == f"Could not read {discs} (Permission denied): {WHAT_TO_DO}"


def test_as_before_a_file_lists_no_audio(discs):
    assert get_audio_files(str(discs / "Disc 1" / "01.flac")) == []


def test_unless_asked_to_skip_it_and_list_the_rest(discs, monkeypatch):
    _fail_scandir(monkeypatch, PermissionError, "Disc 2")
    assert files.album_files(str(discs), [".flac"], skip_unreadable=True) == [str(discs / "Disc 1" / "01.flac")]


def test_as_before_a_folder_gone_mid_walk_is_left_out(discs, monkeypatch):
    _fail_scandir(monkeypatch, FileNotFoundError, "Disc 2")
    assert get_audio_files(str(discs)) == [os.path.join("Disc 1", "01.flac"), os.path.join("Disc 1", "02.mp3")]


@pytest.mark.parametrize("command", ["integrity", "mqa", "upconv", "log"])
def test_a_check_command_stops_on_an_unreadable_disc_folder(discs, monkeypatch, command):
    _fail_scandir(monkeypatch, PermissionError, "Disc 2")
    result = _run(command, str(discs))
    assert isinstance(result.exception, UploadError), result.output
    assert str(result.exception) == _could_not_read(discs, "Disc 2")


def test_transcoding_stops_on_an_unreadable_disc_folder(discs, monkeypatch, tmp_path):
    # Its MP3 is in the folder that can't be read: the check that the album is lossless can't see it either.
    (discs / "Disc 1" / "02.mp3").unlink()
    _fail_scandir(monkeypatch, PermissionError, "Disc 2")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UploadError) as error:
        anyio.run(partial(transcode_folder, str(discs), "V0", output_dir=str(out)))
    assert str(error.value) == _could_not_read(discs, "Disc 2")
    assert list(out.iterdir()) == []


def test_downconverting_stops_on_an_unreadable_disc_folder(discs, monkeypatch, tmp_path):
    (discs / "Disc 1" / "02.mp3").unlink()
    _fail_scandir(monkeypatch, PermissionError, "Disc 2")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UploadError) as error:
        anyio.run(partial(convert_folder, str(discs), output_dir=str(out)))
    assert str(error.value) == _could_not_read(discs, "Disc 2")
    assert list(out.iterdir()) == []
