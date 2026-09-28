"""The upload's MQA check reads every FLAC file, not only the first one."""

import os
from pathlib import Path

import anyio
import asyncclick as click
import av
import av.audio.stream
import numpy as np
import pytest

import salmon.checks.mqa as mqa
from salmon.checks import mqa_test

SAMPLE_RATE = 44100


def _write_flac(path: Path, mqa_marker: bool) -> None:
    """Write 1.5 s of 16-bit stereo noise. With mqa_marker, the MQA syncword is in the lowest bit of
    left XOR right, where the detector looks once the samples are widened to 32 bits."""
    rng = np.random.default_rng(len(path.name))
    frames = int(SAMPLE_RATE * 1.5)
    left = rng.integers(-8000, 8000, frames, dtype=np.int16)
    right = left.copy()
    if mqa_marker:
        right[1000 : 1000 + len(mqa.MAGIC)] ^= mqa.MAGIC.astype(np.int16)

    with av.open(str(path), "w", format="flac") as container:
        stream = container.add_stream("flac", rate=SAMPLE_RATE, layout="stereo")
        assert isinstance(stream, av.audio.stream.AudioStream)
        frame = av.AudioFrame.from_ndarray(np.stack([left, right]), format="s16p", layout="stereo")
        frame.sample_rate = SAMPLE_RATE
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


@pytest.fixture
def album(tmp_path) -> Path:
    """Five tracks, MQA on track 3 only."""
    for number in range(1, 6):
        _write_flac(tmp_path / f"{number:02d}. Track.flac", mqa_marker=number == 3)
    return tmp_path


@pytest.fixture
def checked(monkeypatch) -> list[str]:
    """The names of the files the MQA detector reads."""
    names: list[str] = []
    real_check_mqa = mqa.check_mqa

    async def spy(path: str) -> bool:
        names.append(os.path.basename(path))
        return await real_check_mqa(path)

    monkeypatch.setattr(mqa, "check_mqa", spy)
    return names


def test_the_synthetic_marker_is_detected_on_its_track_only(album) -> None:
    found = {path.name: anyio.run(mqa.check_mqa, str(path)) for path in sorted(album.iterdir())}

    assert found == {f"{n:02d}. Track.flac": n == 3 for n in range(1, 6)}


def test_mqa_on_track_3_stops_the_upload(album, checked, capsys) -> None:
    with pytest.raises(click.Abort):
        anyio.run(mqa_test, str(album))

    assert sorted(checked) == [f"{n:02d}. Track.flac" for n in range(1, 6)]
    reported = [line for line in click.unstyle(capsys.readouterr().out).splitlines() if "MQA syncword" in line]
    assert len(reported) == 1
    assert "03. Track.flac" in reported[0]


def test_an_album_without_mqa_passes(tmp_path, checked) -> None:
    for number in range(1, 4):
        _write_flac(tmp_path / f"{number:02d}. Track.flac", mqa_marker=False)
    (tmp_path / "cover.jpg").write_bytes(b"not audio")

    anyio.run(mqa_test, str(tmp_path))

    assert len(checked) == 3
