"""Upconvert check: 16bit files are skipped quietly, real failures are reported."""

import anyio

import salmon.checks.upconverts as uc
from salmon.errors import UpconvertCheckError, UpconvertCheckNotApplicable


def _check_raising(error):
    async def check(_filepath):
        raise error

    return check


def test_a_16bit_file_is_skipped_without_a_warning(monkeypatch, capsys):
    monkeypatch.setattr(
        uc, "check_upconvert", _check_raising(UpconvertCheckNotApplicable("This is a 16bit FLAC file."))
    )

    assert anyio.run(uc._upconvert_check_handler, "/music/album/01.flac") is None
    assert capsys.readouterr().out == ""


def test_a_real_failure_still_names_the_file_and_the_reason(monkeypatch, capsys):
    monkeypatch.setattr(
        uc, "check_upconvert", _check_raising(UpconvertCheckError("File appears to be corrupt: bad frame"))
    )

    assert anyio.run(uc._upconvert_check_handler, "/music/album/02.flac") is None
    printed = capsys.readouterr().out
    assert "02.flac" in printed
    assert "bad frame" in printed


def test_a_folder_of_16bit_files_gets_one_line_saying_the_check_skips_16bit(monkeypatch, tmp_path, capsys):
    for number in range(1, 4):
        (tmp_path / f"0{number}.flac").write_bytes(b"")
    monkeypatch.setattr(
        uc, "check_upconvert", _check_raising(UpconvertCheckNotApplicable("This is a 16bit FLAC file."))
    )

    assert anyio.run(uc.test_upconverted, str(tmp_path)) is None

    lines = [line for line in capsys.readouterr().out.splitlines() if "skips 16bit" in line]
    assert len(lines) == 1
    assert ".flac" not in lines[0]


def test_a_corrupt_folder_does_not_claim_the_files_are_16bit(monkeypatch, tmp_path, capsys):
    (tmp_path / "01.flac").write_bytes(b"")
    monkeypatch.setattr(uc, "check_upconvert", _check_raising(UpconvertCheckError("File appears to be corrupt")))

    anyio.run(uc.test_upconverted, str(tmp_path))

    assert "skips 16bit" not in capsys.readouterr().out


def test_a_single_16bit_file_gets_the_line_too(monkeypatch, tmp_path, capsys):
    path = tmp_path / "01.flac"
    path.write_bytes(b"")
    monkeypatch.setattr(
        uc, "check_upconvert", _check_raising(UpconvertCheckNotApplicable("This is a 16bit FLAC file."))
    )

    assert anyio.run(uc.test_upconverted, str(path)) is None
    assert capsys.readouterr().out.count("skips 16bit") == 1
