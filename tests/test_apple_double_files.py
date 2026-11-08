"""A macOS "._" metadata file beside an album's files is no track and stays out of what is uploaded.

macOS writes one for each file it copies to a volume that can't hold its metadata (exFAT, SMB). It's named after the
file, so "._01.flac" looks like a track, but mutagen can't read it. Their bytes here are made up.
"""

from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from helpers.tagger_tags import _write_mp3
from helpers.uploader_shortened_conversions import _write_flac

from salmon import cfg, interaction
from salmon.checks import logs, source
from salmon.common.files import album_files, get_audio_files
from salmon.converter import downconverting, transcoding
from salmon.errors import NoncompliantFolderStructure
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.folderstructure import _check_extensions
from salmon.tagger.tags import gather_tags
from salmon.uploader import upload_checks
from salmon.uploader.upload import collect_logfiles

APPLE_DOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\0" * 64


class Answers:
    """Answers the questions it is asked, in order. With none left, it gives the question's default."""

    def __init__(self, *answers: str, assumes: bool = False) -> None:
        self.answers = list(answers)
        self.assumes = assumes
        self.questions: list[str] = []

    async def prompt(self, text: str, **kwargs: Any) -> str:
        self.questions.append(text)
        return self.answers.pop(0) if self.answers else kwargs["default"]

    async def assume_defaults(self) -> bool:
        return self.assumes


def _album(tmp_path: Path, *strays: str) -> Path:
    album = tmp_path / "Artist - Album [WEB FLAC]"
    album.mkdir()
    _write_flac(album / "01.flac", 44100, 16, title="One")
    for stray in strays:
        (album / stray).write_bytes(APPLE_DOUBLE)
    return album


def _check(album: Path, answers: Any, *, scene: bool = False) -> None:
    with interaction.using(answers):
        anyio.run(lambda: _check_extensions(str(album), scene))


def _names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.rglob("*") if path.is_file())


# Reading the album


def test_a_metadata_file_is_not_an_audio_file(tmp_path: Path) -> None:
    assert get_audio_files(str(_album(tmp_path, "._01.flac", "._02.MP3"))) == ["01.flac"]


def test_a_metadata_file_in_a_disc_folder_is_not_an_audio_file(tmp_path: Path) -> None:
    album = _album(tmp_path)
    (album / "CD1").mkdir()
    (album / "CD1" / "._02.flac").write_bytes(APPLE_DOUBLE)

    assert get_audio_files(str(album)) == ["01.flac"]


def test_a_metadata_file_is_not_a_rip_log(tmp_path: Path) -> None:
    album = _album(tmp_path, "._rip.log")
    (album / "rip.log").write_text("log")

    assert album_files(str(album), [".log"]) == [str(album / "rip.log")]


def test_an_album_beside_its_metadata_files_is_read(tmp_path: Path) -> None:
    album = _album(tmp_path, "._01.flac")

    assert list(gather_audio_info(str(album))) == ["01.flac"]
    assert list(gather_tags(str(album))) == ["01.flac"]


def test_a_metadata_file_is_no_log_to_upload(tmp_path: Path) -> None:
    album = _album(tmp_path, "._rip.log")
    (album / "rip.log").write_bytes(b"log")

    assert anyio.run(collect_logfiles, str(album)) == [("rip.log", b"log")]


def test_a_metadata_file_is_no_log_to_score(monkeypatch, tmp_path: Path) -> None:
    album = _album(tmp_path, "._rip.log")
    (album / "rip.log").write_bytes(b"log")
    scored: list[str] = []

    async def score(filepath: str, _path: str) -> None:
        scored.append(Path(filepath).name)

    monkeypatch.setattr(upload_checks, "check_log_cambia", score)

    anyio.run(upload_checks._check_logs, str(album))

    assert scored == ["rip.log"]


def test_a_metadata_file_is_not_a_track_a_log_is_checked_against(tmp_path: Path) -> None:
    album = _album(tmp_path, "._01.flac")

    assert logs._find_audio_files(str(album)) == [str(album / "01.flac")]


# The folder check: what's left in the folder goes in the torrent


@pytest.mark.parametrize("name", ["._01.flac", "._cover.jpg"])
def test_the_folder_check_deletes_a_metadata_file_by_default(tmp_path: Path, name: str) -> None:
    album = _album(tmp_path, name)
    answers = Answers()

    _check(album, answers)

    assert _names(album) == ["01.flac"]
    assert len(answers.questions) == 1
    assert "macOS metadata file" in answers.questions[0]
    assert str(album / name) in answers.questions[0]


def test_continue_does_not_keep_a_metadata_file(tmp_path: Path) -> None:
    album = _album(tmp_path, "._01.flac")
    answers = Answers("c", "d")

    _check(album, answers)

    assert len(answers.questions) == 2
    assert _names(album) == ["01.flac"]


def test_a_metadata_file_can_abort_the_upload(tmp_path: Path) -> None:
    album = _album(tmp_path, "._01.flac")

    with pytest.raises(click.Abort):
        _check(album, Answers("a"))

    assert _names(album) == ["._01.flac", "01.flac"]


def test_assuming_defaults_deletes_a_metadata_file(tmp_path: Path) -> None:
    album = _album(tmp_path, "._01.flac")

    _check(album, Answers(assumes=True))

    assert _names(album) == ["01.flac"]


def test_a_scene_release_with_a_metadata_file_is_refused(tmp_path: Path) -> None:
    album = _album(tmp_path, "._01.flac")

    with pytest.raises(NoncompliantFolderStructure):
        _check(album, Answers(), scene=True)

    assert _names(album) == ["._01.flac", "01.flac"]


def test_a_metadata_file_is_no_second_codec(tmp_path: Path) -> None:
    album = _album(tmp_path, "._01.mp3")
    answers = Answers("d")

    _check(album, answers)

    assert len(answers.questions) == 1
    assert "codecs" not in answers.questions[0]


# Conversions


def test_a_transcode_reads_only_the_tracks(monkeypatch, tmp_path: Path) -> None:
    album = _album(tmp_path, "._01.flac", "._cover.jpg")
    encoded: list[str] = []

    async def encode(_quality: str, flac_path: str, mp3_path: str) -> None:
        encoded.append(Path(flac_path).name)
        _write_mp3(Path(mp3_path))

    monkeypatch.setattr(transcoding, "_flac_to_mp3", encode)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])

    out = anyio.run(lambda: transcoding.transcode_folder(str(album), "V0", output_dir=str(tmp_path)))

    assert encoded == ["01.flac"]
    assert _names(Path(out)) == ["01.mp3"]


def test_a_downconversion_leaves_metadata_files_behind(tmp_path: Path, capsys) -> None:
    album = _album(tmp_path, "._01.flac", "._cover.jpg")
    (album / "cover.jpg").write_bytes(b"jpg")
    output = tmp_path / "out"

    downconverting._copy_extra_files(str(album), str(output), frozenset({str(album / "01.flac")}))

    assert _names(output) == ["cover.jpg"]
    assert "Skip  ._01.flac" in capsys.readouterr().out


def test_a_transcode_leaves_metadata_files_behind(tmp_path: Path, capsys) -> None:
    album = _album(tmp_path, "._cover.jpg")
    (album / "cover.jpg").write_bytes(b"jpg")
    output = tmp_path / "out"

    transcoding._copy_extra_files(str(album), str(output))

    assert _names(output) == ["cover.jpg"]
    assert "Skip  ._cover.jpg" in capsys.readouterr().out


# As before


def test_as_before_a_metadata_file_is_no_proof_of_a_cd_rip(tmp_path: Path) -> None:
    assert source._rip_log(str(_album(tmp_path, "._rip.log"))) is None


@pytest.mark.parametrize("name", [".01.flac", "_.flac", "a._b.flac", "..flac"])
def test_as_before_other_dotted_names_are_audio_files(tmp_path: Path, name: str) -> None:
    album = _album(tmp_path, name)

    assert name in get_audio_files(str(album))


def test_as_before_the_folder_check_asks_about_a_file_with_no_approved_extension(tmp_path: Path) -> None:
    album = _album(tmp_path, "notes.xyz")
    answers = Answers("c")

    _check(album, answers)

    assert "does not have an approved file extension" in answers.questions[0]
    assert _names(album) == ["01.flac", "notes.xyz"]
