"""A .torrent file in an album folder stays out of what is uploaded, and out of a conversion's folder.

The torrent is made from every file in the folder, so the folder check is what keeps one out. The .torrent files here
are made-up bytes, and the runs go against a local fake tracker.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import pytest
from helpers.uploader_dry_run import _album, _run_up
from torf import Torrent

import salmon.images
from salmon import cfg, interaction
from salmon.converter import downconverting, transcoding
from salmon.errors import NoncompliantFolderStructure
from salmon.images.base import BaseImageUploader
from salmon.tagger.folderstructure import _check_extensions, check_folder_structure

MADE_UP = b"d8:announce20:https://made.up/abc4:infode"
TORRENT_NAMES = ["stray.torrent", "STRAY.Torrent", ".torrent"]


class Answers:
    """Answers the questions it is asked, in order, and keeps them. With none left, it gives the question's default."""

    def __init__(self, *answers: str, assumes: bool = False) -> None:
        self.answers = list(answers)
        self.assumes = assumes
        self.questions: list[str] = []

    async def prompt(self, text: str, **kwargs: Any) -> str:
        self.questions.append(text)
        return self.answers.pop(0) if self.answers else kwargs["default"]

    async def confirm(self, text: str, **_kwargs: Any) -> bool:
        raise AssertionError(f"Not a question for this test: {text}")

    async def edit(self, text: str, **_kwargs: Any) -> str:
        raise AssertionError("Not a question for this test")

    async def show_spectrals(self, spectrals_path: Any, all_spectral_ids: Any) -> None:
        raise AssertionError("Not a question for this test")

    async def assume_defaults(self) -> bool:
        return self.assumes


def _release(tmp_path: Path, stray: str) -> tuple[Path, Path]:
    root = tmp_path / "Artist - Album [WEB FLAC]"
    root.mkdir()
    (root / "01. Plain.flac").write_bytes(b"fLaC")
    file = root / stray
    file.write_bytes(MADE_UP)
    return root, file


def _check(root: Path, answers: Answers, *, scene: bool = False) -> None:
    with interaction.using(answers):
        anyio.run(lambda: _check_extensions(str(root), scene))


# --- The question ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", TORRENT_NAMES)
def test_continue_does_not_keep_a_torrent_file(tmp_path: Path, name: str) -> None:
    root, file = _release(tmp_path, name)
    answers = Answers("c", "d")

    _check(root, answers)

    assert not file.exists()
    assert (root / "01. Plain.flac").exists()
    assert len(answers.questions) == 2


def test_the_question_on_a_torrent_file_does_not_offer_continue(tmp_path: Path) -> None:
    root, file = _release(tmp_path, "stray.torrent")
    answers = Answers("d")

    _check(root, answers)

    assert not file.exists()
    assert str(file) in answers.questions[0]
    assert "[D]elete or [a]bort?" in answers.questions[0]
    assert "ontinue" not in answers.questions[0]


def test_abort_after_continue_on_a_torrent_file_leaves_it(tmp_path: Path) -> None:
    root, file = _release(tmp_path, "stray.torrent")
    answers = Answers("c", "a")

    with pytest.raises(click.Abort):
        _check(root, answers)

    assert file.read_bytes() == MADE_UP
    assert len(answers.questions) == 2


def test_as_before_a_torrent_file_goes_on_the_default_answer(tmp_path: Path) -> None:
    root, file = _release(tmp_path, "stray.torrent")
    answers = Answers()

    _check(root, answers)

    assert not file.exists()
    assert len(answers.questions) == 1


@pytest.mark.parametrize("name", ["notes.xyz", "mytorrent", "stray.torrents"])
def test_as_before_any_other_file_may_stay(tmp_path: Path, name: str) -> None:
    root, file = _release(tmp_path, name)
    answers = Answers("c")

    _check(root, answers)

    assert file.exists()
    assert answers.questions == [f"{file} does not have an approved file extension. [D]elete, [a]bort, or [c]ontinue?"]


@pytest.mark.parametrize("name", TORRENT_NAMES)
def test_as_before_yyy_deletes_a_torrent_file_without_asking(tmp_path: Path, capsys, name: str) -> None:
    root, file = _release(tmp_path, name)

    answers = Answers(assumes=True)
    with interaction.using(answers):
        anyio.run(lambda: check_folder_structure(str(root), False))

    assert not file.exists()
    assert answers.questions == []
    assert f"-yyy: deleted {file}" in capsys.readouterr().out


def test_as_before_a_scene_release_with_a_torrent_file_is_reported_and_left(tmp_path: Path, capsys) -> None:
    root, file = _release(tmp_path, "stray.torrent")
    answers = Answers()

    with pytest.raises(NoncompliantFolderStructure):
        _check(root, answers, scene=True)

    assert file.read_bytes() == MADE_UP
    assert answers.questions == []
    assert str(file) in capsys.readouterr().out


# --- A conversion's folder ------------------------------------------------------------------


def _convert_source(tmp_path: Path, stray: str) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / "CD1").mkdir(parents=True)
    (source / "01.flac").write_bytes(b"fLaC")
    (source / "cover.jpg").write_bytes(b"jpeg")
    (source / "notes.txt").write_text("notes")
    (source / stray).write_bytes(MADE_UP)
    (source / "CD1" / stray).write_bytes(MADE_UP)
    return source, tmp_path / "output"


def _copied(output: Path) -> list[str]:
    return sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())


@pytest.mark.parametrize("name", TORRENT_NAMES)
def test_a_transcode_leaves_a_torrent_file_behind(tmp_path: Path, capsys, name: str) -> None:
    source, output = _convert_source(tmp_path, name)

    transcoding._copy_extra_files(str(source), str(output))

    assert _copied(output) == ["cover.jpg", "notes.txt"]
    assert f"Skip  {name}" in capsys.readouterr().out


@pytest.mark.parametrize("name", TORRENT_NAMES)
def test_a_downconversion_leaves_a_torrent_file_behind(tmp_path: Path, capsys, name: str) -> None:
    source, output = _convert_source(tmp_path, name)
    converted = frozenset({str(source / "01.flac")})

    downconverting._copy_extra_files(str(source), str(output), converted)

    assert _copied(output) == ["cover.jpg", "notes.txt"]
    assert f"Skip  {name}" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["mytorrent", "stray.torrents", "stray.torrent.txt"])
def test_as_before_a_conversion_copies_a_name_that_only_looks_like_one(tmp_path: Path, name: str) -> None:
    source, output = _convert_source(tmp_path, name)
    converted = frozenset({str(source / "01.flac")})

    transcoding._copy_extra_files(str(source), str(output / "t"))
    downconverting._copy_extra_files(str(source), str(output / "d"), converted)

    for copy in (output / "t", output / "d"):
        assert (copy / name).read_bytes() == MADE_UP
        assert (copy / "CD1" / name).read_bytes() == MADE_UP


def test_as_before_a_transcode_still_skips_what_it_skipped(tmp_path: Path) -> None:
    source, output = _convert_source(tmp_path, "rip.log")
    (source / "disc.cue").write_text("cue")

    transcoding._copy_extra_files(str(source), str(output))

    assert _copied(output) == ["cover.jpg", "notes.txt"]


def test_as_before_essential_only_copies_images_and_no_torrent_file(tmp_path: Path) -> None:
    source, output = _convert_source(tmp_path, "stray.torrent")
    converted = frozenset({str(source / "01.flac")})

    transcoding._copy_extra_files(str(source), str(output / "t"), essential_only=True)
    downconverting._copy_extra_files(str(source), str(output / "d"), converted, essential_only=True)

    assert _copied(output) == ["d/cover.jpg", "t/cover.jpg"]


# --- What the tracker gets ------------------------------------------------------------------


def _answering(*answers: str):
    """The real folder check, with the questions on bad extensions answered in order."""

    async def check(path: str, scene: bool, **kwargs: Any) -> None:
        with interaction.using(Answers(*answers)):
            await check_folder_structure(path, scene, **kwargs)

    return check


def _files_sent(run: Any) -> list[list[str]]:
    """The names in each torrent the fake tracker was sent."""
    uploads = [sent for sent in run.tracker.sent if sent.query.get("action") == "upload"]
    return [
        sorted(file.name for file in Torrent.read_stream(dict(sent.fields)["file_input"][1]).files) for sent in uploads
    ]


@pytest.fixture
def dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    library, downloads, torrents = tmp_path / "library", tmp_path / "downloads", tmp_path / "torrents"
    for folder in (library, downloads, torrents):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    return library, downloads, torrents


@pytest.fixture(autouse=True)
def image_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake cover and spectrals host, so an upload's images go nowhere."""

    class ImageUploader(BaseImageUploader):
        async def upload_file(self, filename: str) -> tuple[str, None]:
            return f"https://images.test/{os.path.basename(filename)}", None

    monkeypatch.setitem(salmon.images.HOSTS, "testhost", SimpleNamespace(ImageUploader=ImageUploader))
    monkeypatch.setattr(cfg.image, "cover_uploader", "testhost")
    monkeypatch.setattr(cfg.image, "specs_uploader", "testhost")


def _assert_album_sent_without_a_torrent_file(run: Any) -> None:
    """The first torrent is the album's own, by a file only it holds; none of them holds a .torrent file."""
    assert run.result.exit_code == 0, run.result.output
    sent = _files_sent(run)
    assert "cover.jpg" in sent[0]
    assert not [name for names in sent for name in names if name.lower().endswith(".torrent")]


def test_continue_sends_no_torrent_file_to_the_tracker(monkeypatch, dirs) -> None:
    _library, downloads, torrents = dirs
    album = _album(downloads / "Album")
    (album / "stray.torrent").write_bytes(MADE_UP)

    run = _run_up(monkeypatch, album, torrents, check_folder_structure=_answering("c", "d"))

    _assert_album_sent_without_a_torrent_file(run)


def test_as_before_delete_on_a_library_album_goes_from_the_copy_only(monkeypatch, dirs) -> None:
    library, _downloads, torrents = dirs
    album = _album(library / "Album")
    stray = album / "stray.torrent"
    stray.write_bytes(MADE_UP)

    run = _run_up(monkeypatch, album, torrents, check_folder_structure=_answering("d"))

    _assert_album_sent_without_a_torrent_file(run)
    assert stray.read_bytes() == MADE_UP


def test_a_macos_metadata_file_reaches_no_tracker(monkeypatch, dirs) -> None:
    _library, downloads, torrents = dirs
    album = _album(downloads / "Album")
    (album / "._01.flac").write_bytes(b"\x00\x05\x16\x07")

    run = _run_up(monkeypatch, album, torrents, check_folder_structure=_answering("d"))

    assert run.result.exit_code == 0, run.result.output
    assert not [name for names in _files_sent(run) for name in names if name.startswith("._")]
