"""A failed encoder, sox or copy ends the conversion with one error naming the track, not an exception group."""

import errno
import io
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from helpers.tagger_tags import _write_mp3
from helpers.uploader_dry_run import FakeTracker
from helpers.uploader_dry_run import _album as _tagged_album
from helpers.uploader_shortened_conversions import _write_flac
from test_run_exit_status import (
    TWO,
    Row,
    _exit_of_main,
    _main_up_apart,
    _upload_posts,
    dirs,  # noqa: F401 (a fixture)
    no_waits,  # noqa: F401 (a fixture, autouse)
)
from test_uploader_dry_run import image_uploads  # noqa: F401 (a fixture)

from salmon import cfg
from salmon.converter import downconverting, transcoding
from salmon.converter.transcoding import transcode_folder
from salmon.errors import UploadError
from salmon.tagger.audio_info import gather_audio_info
from salmon.uploader import conversion_upload, spectrals

FAILS = 'echo "No space left on device" >&2; exit 1'


def _programs(tmp_path: Path, monkeypatch, **scripts: str) -> None:
    """Only these programs on PATH, so that any other one is not installed."""
    folder = tmp_path / "bin"
    folder.mkdir()
    for name, body in scripts.items():
        script = folder / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(folder))


def _album(tmp_path: Path, names: tuple[str, ...] = ("01. one",), rate: int = 44100, bits: int = 16) -> Path:
    album = tmp_path / "source" / "Album [WEB FLAC]"
    album.mkdir(parents=True)
    for name in names:
        _write_flac(album / f"{name}.flac", rate, bits, title=name)
    return album


@pytest.fixture(autouse=True)
def no_library(monkeypatch) -> None:
    monkeypatch.setattr(cfg.directory, "library_dirs", [])


def _transcode(album: Path, tmp_path: Path) -> str:
    return anyio.run(lambda: transcoding.transcode_folder(str(album), "V0", output_dir=str(tmp_path)))


def _downconvert(album: Path, tmp_path: Path) -> tuple[int | None, str]:
    return anyio.run(lambda: downconverting.convert_folder(str(album), output_dir=str(tmp_path)))


def _make_spectrals(album: Path, folder: Path) -> list[tuple[int, str]]:
    folder.mkdir()
    audio_info = gather_audio_info(str(album))
    return anyio.run(spectrals._run_sox, str(album), list(audio_info), str(folder), audio_info)


def _files_in(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.rglob("*") if path.is_file())


# Where conversions are run


@pytest.mark.parametrize(
    ("scripts", "said"),
    [
        ({"flac": "exit 0", "lame": FAILS}, "No space left on device"),
        ({"flac": "exit 3", "lame": "exit 0"}, "code 3"),
        ({"flac": r"printf 'caf\351' >&2; exit 3", "lame": "exit 0"}, "code 3"),
        ({"flac": "exit 0"}, "lame"),
        ({"lame": "exit 0"}, "flac"),
        ({"flac": "exec /usr/bin/yes", "lame": "exit 0"}, "code -13"),
        ({"flac": "exit 0", "lame": "exit 7"}, "code 7"),
        ({"flac": "exit 0", "lame": r"printf 'caf\351' >&2; exit 1"}, "caf"),
    ],
    ids=[
        "lame_fails",
        "flac_fails",
        "flac_not_utf8",
        "no_lame",
        "no_flac",
        "flac_sigpipe_lame_ok",
        "lame_says_nothing",
        "lame_not_utf8",
    ],
)
def test_a_failed_encoder_is_an_upload_error_naming_the_track(monkeypatch, tmp_path, scripts, said) -> None:
    album = _album(tmp_path)
    _programs(tmp_path, monkeypatch, **scripts)

    with pytest.raises(UploadError) as raised:
        _transcode(album, tmp_path)

    assert "01. one.flac" in str(raised.value)
    assert said in str(raised.value)


def test_an_encoder_that_writes_no_mp3_is_an_upload_error_naming_the_track(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path)
    _programs(tmp_path, monkeypatch, flac="exit 0", lame="exit 0")

    with pytest.raises(UploadError, match=r"01\. one\.flac"):
        _transcode(album, tmp_path)


def _encodes_at_once(monkeypatch) -> None:
    """Every track's encoder fails together: each is held, past any cancel, until all have started."""
    monkeypatch.setattr(cfg.upload, "simultaneous_threads", 3)
    started: list[str] = []
    every_track_started = anyio.Event()

    async def encode(_quality: str, flac_path: str, _mp3_path: str) -> None:
        started.append(flac_path)
        if len(started) == 3:
            every_track_started.set()
        with anyio.CancelScope(shield=True):
            await every_track_started.wait()
        raise UploadError(f"{os.path.basename(flac_path)} failed")

    monkeypatch.setattr(transcoding, "_flac_to_mp3", encode)


def test_several_tracks_failing_together_are_all_named(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path, ("01. one", "02. two", "03. three"))
    _encodes_at_once(monkeypatch)

    with pytest.raises(UploadError) as raised:
        _transcode(album, tmp_path)

    assert sorted(str(raised.value).splitlines()) == [
        "01. one.flac failed",
        "02. two.flac failed",
        "03. three.flac failed",
    ]


def test_a_failed_rename_leaves_no_partial_mp3_and_names_the_track(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path)
    _programs(tmp_path, monkeypatch)

    async def encode(_quality: str, _flac_path: str, mp3_path: str) -> None:
        _write_mp3(Path(mp3_path))

    def fail(*_args: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(transcoding, "_flac_to_mp3", encode)
    monkeypatch.setattr(transcoding.os, "replace", fail)

    with pytest.raises(UploadError, match=r"01\. one\.flac.*No space left"):
        _transcode(album, tmp_path)

    assert _files_in(Path(transcoding._build_output_path(str(album), "V0", str(tmp_path)))) == []


@pytest.mark.parametrize(
    ("scripts", "said"),
    [({"sox": "echo FAIL >&2; exit 2"}, "code 2"), ({"sox": r"printf 'caf\351' >&2; exit 2"}, "code 2"), ({}, "sox")],
    ids=["sox_fails", "not_utf8", "no_sox"],
)
def test_a_failed_sox_conversion_is_an_upload_error_naming_the_track(monkeypatch, tmp_path, scripts, said) -> None:
    album = _album(tmp_path, bits=24)
    _programs(tmp_path, monkeypatch, **scripts)

    with pytest.raises(UploadError) as raised:
        _downconvert(album, tmp_path)

    assert "01. one.flac" in str(raised.value)
    assert said in str(raised.value)


@pytest.mark.parametrize(
    ("scripts", "said"),
    [
        ({"sox": 'echo "sox FAIL formats" >&2; exit 2'}, "sox FAIL formats"),
        ({"sox": r"printf 'caf\351' >&2; exit 2"}, "caf"),
        ({"sox": "exit 2"}, "code 2"),
        ({}, "sox"),
    ],
    ids=["sox_fails", "not_utf8", "says_nothing", "no_sox"],
)
def test_a_failed_spectral_is_an_upload_error_naming_the_track(monkeypatch, tmp_path, scripts, said) -> None:
    album = _album(tmp_path)
    _programs(tmp_path, monkeypatch, **scripts)

    with pytest.raises(UploadError) as raised:
        _make_spectrals(album, tmp_path / "Spectrals")

    assert "01. one.flac" in str(raised.value)
    assert said in str(raised.value)


def test_lames_reason_is_given_when_flac_dies_of_the_broken_pipe(tmp_path, monkeypatch) -> None:
    album = _album(tmp_path)
    _programs(tmp_path, monkeypatch, flac="exec /usr/bin/yes", lame='echo "lame: disk full" >&2; exit 1')

    with pytest.raises(UploadError, match="disk full"):
        _transcode(album, tmp_path)


# As before


def test_as_before_a_failure_beside_a_bug_stays_an_exception_group(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path, ("01. one", "02. two"))
    monkeypatch.setattr(cfg.upload, "simultaneous_threads", 2)
    started: list[str] = []
    both_started = anyio.Event()

    async def encode(_quality: str, flac_path: str, _mp3_path: str) -> None:
        started.append(flac_path)
        if len(started) == 2:
            both_started.set()
        with anyio.CancelScope(shield=True):
            await both_started.wait()
        raise (UploadError if flac_path.endswith("01. one.flac") else KeyError)("failed")

    monkeypatch.setattr(transcoding, "_flac_to_mp3", encode)

    with pytest.raises(ExceptionGroup):
        _transcode(album, tmp_path)


# Through salmon's own commands


def _salmon(monkeypatch, capsys, *args: str) -> tuple[Any, str, str]:
    monkeypatch.setattr(sys, "argv", ["salmon", *args])
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" * 30))
    code = _exit_of_main()
    captured = capsys.readouterr()
    return code, click.unstyle(captured.out), click.unstyle(captured.err)


def test_salmon_transcode_ends_in_a_clear_error(monkeypatch, capsys, tmp_path) -> None:
    _programs(tmp_path, monkeypatch, flac="exit 0", lame=FAILS)
    album = _tagged_album(tmp_path / "Album [FLAC]")

    code, out, err = _salmon(monkeypatch, capsys, "transcode", str(album), "-b", "V0")

    assert code == 1
    assert "There was an error: " in out
    assert "No space left on device" in out
    assert "Traceback" not in err


def test_salmon_downconv_ends_in_a_clear_error(monkeypatch, capsys, tmp_path) -> None:
    _programs(tmp_path, monkeypatch, sox="exit 2")
    album = _album(tmp_path, bits=24)

    code, out, err = _salmon(monkeypatch, capsys, "downconv", str(album))

    assert code == 1
    assert "There was an error: sox conversion failed for 01. one.flac" in out
    assert "Traceback" not in err


@pytest.mark.parametrize("scripts", [{"sox": "exit 2"}, {}], ids=["sox_fails", "no_sox"])
def test_salmon_specs_ends_in_a_clear_error(monkeypatch, capsys, tmp_path, scripts) -> None:
    _programs(tmp_path, monkeypatch, **scripts)
    album = _tagged_album(tmp_path / "Album")

    code, out, err = _salmon(monkeypatch, capsys, "specs", str(album))

    assert code == 1
    assert "There was an error: " in out
    assert "Traceback" not in err


def _lame_failing_on(tmp_path: Path, monkeypatch, flag: str) -> None:
    """lame failing when called with this flag, and otherwise writing a good MP3 to its last argument."""
    good = tmp_path / "good.mp3"
    _write_mp3(good)
    lame = f'case "$*" in *"{flag}"*) echo "No space left on device" >&2; exit 1;; esac\n'
    lame += f'for last; do :; done\n/bin/cp "{good}" "$last"'
    _programs(tmp_path, monkeypatch, flac="exit 0", lame=lame)


def _up(monkeypatch, capsys, dirs_: tuple[Path, Path], tracker: FakeTracker, **kw: Any) -> tuple[Any, str, str]:
    downloads, torrents = dirs_
    album = _tagged_album(downloads.parent / "seeding" / "Album")
    row = Row(0, 0, "", tracker=lambda: tracker, fakes=lambda: {"transcode_folder": transcode_folder}, **kw)
    return _main_up_apart(monkeypatch, capsys, album, torrents, row, tracker)


@pytest.mark.usefixtures("image_uploads")
def test_up_goes_on_to_the_next_tracker_after_a_failed_conversion(monkeypatch, capsys, dirs, tmp_path) -> None:  # noqa: F811
    _lame_failing_on(tmp_path, monkeypatch, "-V 0")
    tracker = FakeTracker()

    code, out, err = _up(monkeypatch, capsys, dirs, tracker, args=("--conversions", "V0"), trackers=TWO)

    assert code == 3
    assert "Traceback" not in err
    assert _upload_posts(tracker) == 2  # The FLAC on each tracker.
    assert "No space left on device" in out
    assert "01 - one.flac" in out or "02 - two.flac" in out
    # The second tracker's turn finds the folder the first one left: no record, so it is not reused.
    assert "doesn't match the expected transcode" in out


@pytest.mark.usefixtures("image_uploads")
def test_up_goes_on_to_the_next_conversion_after_a_failed_one(monkeypatch, capsys, dirs, tmp_path) -> None:  # noqa: F811
    # salmon makes the 320 first.
    _lame_failing_on(tmp_path, monkeypatch, "-b 320")
    tracker = FakeTracker()

    code, out, err = _up(monkeypatch, capsys, dirs, tracker, args=("--conversions", "V0,320"))

    assert code == 3
    assert "Traceback" not in err
    assert _upload_posts(tracker) == 2  # The FLAC and the V0; never the 320.
    assert "V0 transcode completed" in out


@pytest.mark.parametrize(
    "task",
    [
        {"name": "MP3 V0", "action": "transcode", "encoding": "V0"},
        {"name": "16 bit", "action": "downconvert", "target_bitdepth": 16, "target_sample_rate": 44100},
    ],
    ids=["transcode", "downconvert"],
)
def test_up_ends_only_that_conversion_when_a_copy_fails(monkeypatch, capsys, tmp_path, task) -> None:
    album = _album(tmp_path, bits=24)
    (album / "cover.jpg").write_bytes(b"jpeg")
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))

    def full(*_args: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copy", full)
    nothing: Any = None

    async def run() -> None:
        await conversion_upload.execute_downconversion_tasks(
            [task],
            str(album),
            nothing,
            nothing,
            nothing,
            nothing,
            nothing,
            nothing,
            nothing,
            nothing,
            nothing,
            nothing,
            nothing,
            nothing,
            nothing,
            "WEB",
            "https://tracker.test/1",
            max_path_length=255,
        )

    anyio.run(run)

    assert "No space left on device" in capsys.readouterr().out
