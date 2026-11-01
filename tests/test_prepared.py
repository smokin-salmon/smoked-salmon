"""An upload's local checks take the results a preparation made for its files (#790).

A preparation (salmon web, for a queued upload) runs the checks that only read the album and keeps each file's result
in a Prepared. While the upload runs with it (prepared.using), each check takes a file's result instead of computing
it again, if the file is unchanged: the same path in the album, size and modification time. The integrity check runs
once the files are retagged and renamed, so its results are kept by the file's audio instead (prepared.audio_key).

The checks' slow parts are counted: the MQA and upconvert detectors and the log CRCs patched, sox patched where
salmon calls it, and flac a fake on PATH that records each run.
"""

import os
import shutil
import stat
import sys
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import Any

import anyio
import pytest
from mutagen.flac import FLAC
from test_checks_logs import _one_disc_log  # pyright: ignore[reportMissingImports]
from test_checks_mqa import _write_flac  # pyright: ignore[reportMissingImports]

from salmon import cfg, prepared
from salmon.checks import mqa_test
from salmon.checks.integrity import IntegrityResult
from salmon.checks.upconverts import UpconvertCheckResult
from salmon.prepared import Prepared
from salmon.uploader import preparation, spectrals

# By their modules: salmon.checks has commands of the same names, which hide them as attributes.
integrity = import_module("salmon.checks.integrity")
logs = import_module("salmon.checks.logs")
mqa = import_module("salmon.checks.mqa")
upconverts = import_module("salmon.checks.upconverts")

TRACKS = ["01 One.flac", "02 Two.flac"]

# flac's test mode (-wt): a FLAC stream decodes, anything else fails as flac fails. Each run is recorded in
# $FLAC_RUNS, one line per file.
FAKE_FLAC = """\
import os, sys
path = sys.argv[-1]
with open(os.environ["FLAC_RUNS"], "a") as runs:
    runs.write(" ".join(sys.argv[1:]) + "\\n")
with open(path, "rb") as handle:
    good = handle.read(4) == b"fLaC"
sys.stderr.write(f"{path}: ok\\n" if good else f"{path}: ERROR while decoding data\\n")
sys.exit(0 if good else 1)
"""


@pytest.fixture
def flac_runs(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path_factory.mktemp("bin")
    script = bin_dir / "flac"
    script.write_text(f"#!{sys.executable}\n{FAKE_FLAC}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    runs = tmp_path_factory.mktemp("flac") / "runs"
    runs.touch()
    monkeypatch.setenv("FLAC_RUNS", str(runs))
    return runs


def _flac_runs(runs: Path) -> list[str]:
    return [os.path.basename(line.split(" ", 1)[1]) for line in runs.read_text().splitlines()]


@pytest.fixture
def album(tmp_path: Path) -> Path:
    path = tmp_path / "music" / "Artist - Album"
    path.mkdir(parents=True)
    for name in TRACKS:
        _write_flac(path / name, mqa_marker=False)
    return path


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """The preparation's own folder, where its spectral images go."""
    path = tmp_path / "prepared"
    path.mkdir()
    return path


def _touch(path: Path) -> None:
    """Change a file's modification time, as a tag rewrite would."""
    before = path.stat().st_mtime_ns
    os.utime(path, ns=(before + 5_000_000_000, before + 5_000_000_000))


def _retag(path: Path, **tags: str) -> None:
    tagged = FLAC(path)
    tagged.update(tags)
    tagged.save()


@pytest.fixture
def mqa_checked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    checked: list[str] = []

    async def check_mqa(path: str) -> bool:
        checked.append(os.path.basename(path))
        return False

    monkeypatch.setattr(mqa, "check_mqa", check_mqa)
    return checked


# --- A file's result, by its path, size and modification time ---------------------------------------


def test_mqa_takes_an_unchanged_files_result_and_checks_a_changed_one(album, folder, mqa_checked) -> None:
    kept = Prepared(str(album), str(folder))
    for name in TRACKS:
        kept.keep("mqa", str(album / name), os.stat(album / name), False)
    _touch(album / TRACKS[1])

    with prepared.using(kept):
        anyio.run(mqa_test, str(album))

    assert mqa_checked == [TRACKS[1]]


def test_a_prepared_mqa_hit_is_still_reported(album, folder, mqa_checked, capsys) -> None:
    kept = Prepared(str(album), str(folder))
    kept.keep("mqa", str(album / TRACKS[0]), os.stat(album / TRACKS[0]), True)
    kept.keep("mqa", str(album / TRACKS[1]), os.stat(album / TRACKS[1]), False)

    with prepared.using(kept), pytest.raises(BaseException) as raised:
        anyio.run(mqa_test, str(album))

    assert type(raised.value).__name__ == "Abort"
    assert f"MQA syncword present in '{album / TRACKS[0]}'" in capsys.readouterr().out
    assert mqa_checked == []


def test_nothing_is_taken_outside_the_upload_it_was_prepared_for(album, folder, mqa_checked) -> None:
    kept = Prepared(str(album), str(folder))
    for name in TRACKS:
        kept.keep("mqa", str(album / name), os.stat(album / name), False)

    anyio.run(mqa_test, str(album))

    assert mqa_checked == TRACKS


def test_a_staged_copy_takes_the_results_of_the_album_it_copies(album, folder, mqa_checked, tmp_path) -> None:
    kept = Prepared(str(album), str(folder))
    for name in TRACKS:
        kept.keep("mqa", str(album / name), os.stat(album / name), False)
    # As staged_source copies a library album: copytree keeps the modification times.
    copy = tmp_path / "run" / album.name
    shutil.copytree(album, copy)

    with prepared.using(kept):
        anyio.run(mqa_test, str(copy))

    assert mqa_checked == []


def test_a_file_changed_while_it_was_checked_is_checked_again(album, folder, mqa_checked) -> None:
    kept = Prepared(str(album), str(folder))
    # The state of the file when its check started, which a write during the check left behind.
    before = os.stat(album / TRACKS[0])
    _touch(album / TRACKS[0])
    kept.keep("mqa", str(album / TRACKS[0]), before, False)

    with prepared.using(kept):
        anyio.run(partial(mqa_test, str(album), all_files=False))

    assert mqa_checked == [TRACKS[0]]


def test_the_upconvert_check_names_the_files_it_checks_now(album, folder, monkeypatch, tmp_path, capsys) -> None:
    checked: list[str] = []

    async def check_upconvert(path: str) -> UpconvertCheckResult:
        checked.append(os.path.basename(path))
        return UpconvertCheckResult(filepath=path, is_upconverted=False, wasted_bits=0, bitdepth=24)

    monkeypatch.setattr(upconverts, "check_upconvert", check_upconvert)
    kept = Prepared(str(album), str(folder))
    for name in TRACKS:
        path = str(album / name)
        kept.keep("upconvert", path, os.stat(path), UpconvertCheckResult(path, False, 0, 24))
    copy = tmp_path / "run" / album.name
    shutil.copytree(album, copy)
    _touch(copy / TRACKS[1])

    with prepared.using(kept):
        assert anyio.run(upconverts.test_upconverted, str(copy)) is False

    assert checked == [TRACKS[1]]
    out = capsys.readouterr().out
    assert f"{TRACKS[0]}: does not have a high number of wasted bits (Wasted bits: 0/24)" in out
    assert str(album) not in out


def test_the_log_check_takes_each_files_crc(album, folder, monkeypatch) -> None:
    crcs = {TRACKS[0]: "AAAAAAAA", TRACKS[1]: "BBBBBBBB"}
    calculated: list[str] = []

    async def calculate(path: str, _: Any = None) -> str:
        calculated.append(os.path.basename(path))
        return crcs[os.path.basename(path)]

    monkeypatch.setattr(logs, "_calculate_file_crc_async", calculate)
    (album / "rip.log").write_text("log")
    kept = Prepared(str(album), str(folder))
    for name in TRACKS:
        kept.keep("crc", str(album / name), os.stat(album / name), crcs[name])
    _touch(album / TRACKS[0])

    with prepared.using(kept):
        anyio.run(logs.verify_log_crcs, _one_disc_log("AAAAAAAA", "BBBBBBBB"), str(album / "rip.log"), str(album))

    assert calculated == [TRACKS[0]]


def test_a_prepared_crc_that_does_not_match_fails_the_check(album, folder, monkeypatch) -> None:
    monkeypatch.setattr(logs, "_calculate_file_crc_async", None)  # Not called: both are prepared.
    (album / "rip.log").write_text("log")
    kept = Prepared(str(album), str(folder))
    for name in TRACKS:
        kept.keep("crc", str(album / name), os.stat(album / name), "00000000")

    with prepared.using(kept), pytest.raises(logs.CRCMismatchError):
        anyio.run(logs.verify_log_crcs, _one_disc_log("AAAAAAAA", "BBBBBBBB"), str(album / "rip.log"), str(album))


@pytest.fixture
def sox_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """sox, where salmon calls it: writes the images asked for, each run recorded by its input file."""
    runs: list[str] = []
    run_process = spectrals.anyio.run_process

    async def fake_sox(args: list[str], **kwargs: Any) -> Any:
        if args[0] != "sox":
            return await run_process(args, **kwargs)
        runs.append(os.path.basename(args[2]))
        for i, arg in enumerate(args):
            if arg == "-o":
                Path(args[i + 1]).write_bytes(f"spectral of {os.path.basename(args[2])}".encode())
        return None

    monkeypatch.setattr(spectrals.anyio, "run_process", fake_sox)
    return runs


AUDIO_INFO = {name: {"duration": 60} for name in TRACKS}


def test_the_spectrals_are_copied_from_the_preparation(album, folder, sox_runs, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.compression, "compress_spectrals", False)
    kept = Prepared(str(album), str(folder))
    anyio.run(preparation.prepare_spectrals, kept, AUDIO_INFO)
    assert sox_runs == TRACKS
    _touch(album / TRACKS[1])
    out = tmp_path / "Spectrals"
    out.mkdir()

    with prepared.using(kept):
        ids = anyio.run(spectrals.generate_spectrals_all, str(album), str(out), AUDIO_INFO)

    assert ids == {1: TRACKS[0], 2: TRACKS[1]}
    assert sox_runs == [*TRACKS, TRACKS[1]]
    assert sorted(p.name for p in out.iterdir()) == ["01 Full.png", "01 Zoom.png", "02 Full.png", "02 Zoom.png"]
    assert (out / "01 Full.png").read_bytes() == f"spectral of {TRACKS[0]}".encode()
    # The preparation's own images stay where they are until its folder is removed.
    assert len(list(folder.iterdir())) == 4


# --- The integrity check, by the file's audio ------------------------------------------------------


def test_the_audio_key_leaves_out_the_tags_and_nothing_else(album) -> None:
    track = album / TRACKS[0]
    key = prepared.audio_key(str(track))
    assert key is not None

    _retag(track, title="Another title", comment="x" * 5000)
    os.rename(track, album / "renamed.flac")
    assert prepared.audio_key(str(album / "renamed.flac")) == key

    data = (album / "renamed.flac").read_bytes()
    (album / "renamed.flac").write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    assert prepared.audio_key(str(album / "renamed.flac")) != key


def test_the_audio_key_of_a_file_that_is_no_flac_is_none(tmp_path) -> None:
    (tmp_path / "x.flac").write_bytes(b"ID3 not a flac")
    assert prepared.audio_key(str(tmp_path / "x.flac")) is None
    assert prepared.audio_key(str(tmp_path / "missing.flac")) is None


def test_the_integrity_check_takes_a_clean_pass_after_the_files_were_retagged_and_renamed(
    album, folder, flac_runs
) -> None:
    kept = Prepared(str(album), str(folder))
    anyio.run(preparation.prepare_integrity, kept)
    assert sorted(_flac_runs(flac_runs)) == TRACKS
    flac_runs.write_text("")
    _retag(album / TRACKS[0], title="One")
    os.rename(album / TRACKS[0], album / "01. One.flac")
    _retag(album / TRACKS[1], title="Two")
    data = (album / TRACKS[1]).read_bytes()
    (album / TRACKS[1]).write_bytes(data + b"\0")  # Its audio changed.

    with prepared.using(kept):
        result = anyio.run(integrity.check_integrity, str(album))

    assert result == IntegrityResult(True, checked=2)
    assert _flac_runs(flac_runs) == [TRACKS[1]]


def test_a_failed_integrity_check_is_never_taken(album, folder, flac_runs) -> None:
    bad = album / "03 Bad.flac"
    bad.write_bytes(b"not a flac stream")
    kept = Prepared(str(album), str(folder))
    anyio.run(preparation.prepare_integrity, kept)
    flac_runs.write_text("")

    with prepared.using(kept):
        result = anyio.run(integrity.check_integrity, str(album))

    assert not result.passed
    assert result.decode_failures == ("03 Bad.flac",)
    assert _flac_runs(flac_runs) == ["03 Bad.flac"]


# --- A preparation ---------------------------------------------------------------------------


def test_a_preparation_never_writes_into_the_album(album, folder, flac_runs, sox_runs, mqa_checked) -> None:
    (album / "rip.log").write_text("log")
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in album.iterdir()}
    kept = Prepared(str(album), str(folder))

    anyio.run(
        partial(
            preparation.prepare_upload,
            kept,
            source="CD",
            skip_mqa=False,
            skip_up=False,
            skip_log_check=False,
            skip_integrity_check=False,
            spectrals_after=False,
            spectral_ids=(),
        )
    )

    assert {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in album.iterdir()} == before
    assert sox_runs == TRACKS
    assert mqa_checked == TRACKS
    assert sorted(_flac_runs(flac_runs)) == TRACKS


@pytest.mark.parametrize(
    ("options", "kept"),
    [
        ({}, {"mqa", "crc", "spectral", "integrity"}),
        ({"skip_mqa": True}, {"crc", "spectral", "integrity"}),
        ({"source": "WEB"}, {"mqa", "spectral", "integrity"}),
        ({"skip_log_check": True}, {"mqa", "spectral", "integrity"}),
        ({"spectrals_after": True}, {"mqa", "crc", "integrity"}),
        ({"spectral_ids": (0,)}, {"mqa", "crc", "integrity"}),
        ({"skip_integrity_check": True}, {"mqa", "crc", "spectral"}),
    ],
)
def test_a_preparation_makes_what_the_upload_will_check(
    album, folder, flac_runs, sox_runs, mqa_checked, monkeypatch, options: dict[str, Any], kept: set[str]
) -> None:
    monkeypatch.setattr(logs, "_calculate_file_crc_async", lambda *_a: _crc())
    (album / "rip.log").write_text("log")
    made = Prepared(str(album), str(folder))
    arguments = {
        "source": "CD",
        "skip_mqa": False,
        "skip_up": False,
        "skip_log_check": False,
        "skip_integrity_check": False,
        "spectrals_after": False,
        "spectral_ids": (),
        **options,
    }

    anyio.run(partial(preparation.prepare_upload, made, **arguments))

    # 16bit files: the upconvert check skips them, so there is nothing to keep.
    assert made.checks() == kept


async def _crc() -> str:
    return "AAAAAAAA"


def test_a_failed_step_leaves_no_result_and_the_others_go_on(album, folder, flac_runs, sox_runs, monkeypatch) -> None:
    async def broken(_path: str) -> bool:
        raise RuntimeError("no PyAV")

    monkeypatch.setattr(mqa, "check_mqa", broken)
    made = Prepared(str(album), str(folder))

    anyio.run(
        partial(
            preparation.prepare_upload,
            made,
            source="WEB",
            skip_mqa=False,
            skip_up=True,
            skip_log_check=True,
            skip_integrity_check=False,
            spectrals_after=False,
            spectral_ids=(),
        )
    )

    assert made.checks() == {"spectral", "integrity"}
