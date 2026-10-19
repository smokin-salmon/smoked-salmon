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

    output = capsys.readouterr().out
    assert "skips 16bit" not in output


def test_a_single_16bit_file_gets_the_line_too(monkeypatch, tmp_path, capsys):
    path = tmp_path / "01.flac"
    path.write_bytes(b"")
    monkeypatch.setattr(
        uc, "check_upconvert", _check_raising(UpconvertCheckNotApplicable("This is a 16bit FLAC file."))
    )

    assert anyio.run(uc.test_upconverted, str(path)) is None
    output = capsys.readouterr().out
    assert output.count("skips 16bit") == 1


def _flac_folder(tmp_path, count=2):
    for number in range(1, count + 1):
        (tmp_path / f"0{number}.flac").write_bytes(b"")
    return str(tmp_path)


def _upload_test_output(capsys, path):
    anyio.run(uc.upload_upconvert_test, path)
    return capsys.readouterr().out


def test_the_upload_check_does_not_claim_no_upconverts_when_every_file_is_16bit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        uc, "check_upconvert", _check_raising(UpconvertCheckNotApplicable("This is a 16bit FLAC file."))
    )

    output = _upload_test_output(capsys, _flac_folder(tmp_path))

    assert "No upconverts detected" not in output
    assert "skips 16bit" in output


def test_the_upload_check_does_not_claim_no_upconverts_when_every_file_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(uc, "check_upconvert", _check_raising(UpconvertCheckError("File appears to be corrupt")))

    output = _upload_test_output(capsys, _flac_folder(tmp_path))

    assert "No upconverts detected" not in output
    assert "corrupt" in output


def test_the_upload_check_says_so_when_there_is_no_flac_file(tmp_path, capsys):
    output = _upload_test_output(capsys, str(tmp_path))

    assert "No upconverts detected" not in output
    assert "No FLAC files found" in output


def test_the_upload_check_still_reports_no_upconverts_when_files_were_checked(monkeypatch, tmp_path, capsys):
    async def clean(filepath):
        return uc.UpconvertCheckResult(filepath=filepath, is_upconverted=False, wasted_bits=0, bitdepth=24)

    monkeypatch.setattr(uc, "check_upconvert", clean)

    assert "No upconverts detected" in _upload_test_output(capsys, _flac_folder(tmp_path))
