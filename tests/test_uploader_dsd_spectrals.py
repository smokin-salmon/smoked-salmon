"""The spectrals and frequency analysis of DSD files (#919): PyAV decodes them to PCM at 4x their base rate, which sox
reads on stdin, told how long the track is. sox here is a fake that notes what it was given."""

import json
import os
import stat
import sys
from pathlib import Path

import anyio
import pytest
from helpers.dsd import DSD64, DSD256, write_dff, write_dsf

from salmon.errors import UploadError
from salmon.uploader import frequency
from salmon.uploader.spectrals import _generate_spectral_for_file, _run_sox, spectrals_in_background

FAKE_SOX = """
import json, os, sys
args = sys.argv[1:]
read = 0
if os.environ.get("SOX_MODE") != "exit early" and "-" in args:
    while chunk := sys.stdin.buffer.read(1 << 16):
        read += len(chunk)
with open(os.environ["SOX_NOTES"], "w") as f:
    json.dump({"args": args, "read": read}, f)
if os.environ.get("SOX_MODE") in ("fail", "exit early"):
    sys.exit("sox FAIL spectrogram: something")
for i, arg in enumerate(args):
    if arg == "-o":
        with open(args[i + 1], "wb") as f:
            f.write(b"PNG")
"""


@pytest.fixture
def sox(tmp_path_factory, monkeypatch) -> Path:
    """Put the fake sox first on PATH; give the file its notes go to."""
    root = tmp_path_factory.mktemp("sox")
    script = root / "sox"
    script.write_text(f"#!{sys.executable}\n{FAKE_SOX}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{root}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SOX_NOTES", str(root / "notes.json"))
    return root / "notes.json"


def _make(album: Path, name: str, out: Path) -> tuple[int, str]:
    info = {name: {"duration": 1}}
    return anyio.run(_generate_spectral_for_file, str(album), name, str(out), info, 0)


@pytest.mark.parametrize(
    ("write", "name", "rate", "pcm"),
    [
        (write_dsf, "01.dsf", DSD64, 176400),
        (write_dff, "01.dff", DSD256, 176400),
        (write_dsf, "01.dsf", 3072000, 192000),
    ],
    ids=["DSD64 dsf", "DSD256 dff", "48k family"],
)
def test_a_dsd_files_samples_reach_sox_as_pcm(tmp_path, sox, write, name: str, rate: int, pcm: int) -> None:
    write(tmp_path / name, rate=rate, seconds=0.2)
    out = tmp_path / "specs"
    out.mkdir()

    assert _make(tmp_path, name, out) == (1, name)

    notes = json.loads(sox.read_text())
    raw = ["--multi-threaded", "-t", "raw", "-r", str(pcm), "-e", "floating-point", "-b", "32", "-L", "-c", "2", "-"]
    assert notes["args"][: len(raw)] == raw
    # The full spectral is told the track's length: sox cannot know it from a pipe, and would show 20 seconds.
    full = notes["args"][: notes["args"].index("-o")]
    assert full[full.index("-d") + 1] == "0.200"
    # 0.2 seconds of two channels of 32-bit floats, give or take the resampler's edges.
    assert abs(notes["read"] - round(0.2 * pcm) * 2 * 4) <= 64 * 8
    assert sorted(os.listdir(out)) == ["01 Full.png", "01 Zoom.png"]


def test_a_flac_file_is_still_given_to_sox_by_its_path(tmp_path, sox) -> None:
    (tmp_path / "01.flac").write_bytes(b"fLaC")

    _make(tmp_path, "01.flac", tmp_path)

    args = json.loads(sox.read_text())["args"]
    assert args[:2] == ["--multi-threaded", str(tmp_path / "01.flac")]
    assert "-d" not in args[: args.index("-o")]


@pytest.mark.parametrize("mode", ["fail", "exit early"])
def test_a_sox_that_fails_on_dsd_says_why(tmp_path, sox, monkeypatch, mode: str) -> None:
    # Long enough to fill the pipe: a sox that stops reading must not leave the feed waiting.
    write_dsf(tmp_path / "01.dsf", seconds=3)
    monkeypatch.setenv("SOX_MODE", mode)

    with pytest.raises(UploadError, match="sox could not make the spectrals of 01.dsf: sox FAIL spectrogram"):
        anyio.run(_run_sox, str(tmp_path), ["01.dsf"], str(tmp_path), {"01.dsf": {"duration": 3}})


def test_a_dsd_file_that_does_not_decode_says_so(tmp_path, sox) -> None:
    (tmp_path / "01.dsf").write_bytes(b"DSD " + bytes(200))

    with pytest.raises(UploadError, match="Could not decode 01.dsf for its spectrals"):
        _make(tmp_path, "01.dsf", tmp_path)
    assert not sox.exists()


def test_spectrals_are_made_in_the_background_for_a_dsd_release(tmp_path, sox) -> None:
    write_dsf(tmp_path / "01.dsf")

    async def run() -> object:
        async with spectrals_in_background(str(tmp_path), {"01.dsf": {"duration": 1}}, (), "DSD") as generation:
            return generation

    assert anyio.run(run) is not None


def test_the_frequency_analysis_decodes_dsd_at_4_times_its_base_rate(tmp_path) -> None:
    write_dsf(tmp_path / "01.dsf", rate=DSD256, seconds=0.2)

    samples, rate = frequency.decode_mono(str(tmp_path / "01.dsf"))

    assert rate == 176400
    assert abs(len(samples) - round(0.2 * 176400)) <= 64
