"""The upload's MQA check reads every FLAC file, not only the first one."""

import os
from pathlib import Path

import anyio
import asyncclick as click
import pytest
from helpers.checks_mqa import _write_flac

import salmon.checks.mqa as mqa
from salmon import cfg
from salmon.checks import mqa_test


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


def test_every_file_is_the_default() -> None:
    assert cfg.upload.mqa_check_all_tracks is True


def test_with_the_opt_out_only_the_first_file_is_read(album, checked) -> None:
    """upload.mqa_check_all_tracks = false: faster, and MQA on track 3 is not caught."""
    anyio.run(lambda: mqa_test(str(album), all_files=False))

    assert checked == ["01. Track.flac"]


def test_an_album_without_mqa_passes(tmp_path, checked) -> None:
    for number in range(1, 4):
        _write_flac(tmp_path / f"{number:02d}. Track.flac", mqa_marker=False)
    (tmp_path / "cover.jpg").write_bytes(b"not audio")

    anyio.run(mqa_test, str(tmp_path))

    assert len(checked) == 3
