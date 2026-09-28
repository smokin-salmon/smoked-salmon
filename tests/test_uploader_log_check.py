import os

import anyio
import asyncclick as click
import pytest

import salmon.uploader as uploader
from salmon.errors import LogCheckSkipped


def _album_with_a_log(tmp_path) -> str:
    (tmp_path / "rip.log").write_text("log")
    return str(tmp_path)


def _check_log_raising(error: BaseException):
    async def check_log_cambia(_logpath: str, _basepath: str) -> None:
        raise error

    return check_log_cambia


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(13, "Permission denied"),
        RuntimeError("Error decoding audio"),
        ExceptionGroup("CRC workers failed", [OSError("read failed")]),
    ],
    ids=["unreadable audio", "undecodable audio", "failed CRC workers"],
)
def test_a_failed_verification_aborts_the_upload(tmp_path, monkeypatch, error) -> None:
    monkeypatch.setattr(uploader, "check_log_cambia", _check_log_raising(error))
    with pytest.raises(click.Abort):
        anyio.run(uploader._check_logs, _album_with_a_log(tmp_path))


def test_a_log_that_cannot_be_checked_is_reported_not_fatal(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(uploader, "check_log_cambia", _check_log_raising(LogCheckSkipped("No audio files found!")))
    anyio.run(uploader._check_logs, _album_with_a_log(tmp_path))
    out = capsys.readouterr().out
    assert "Log not checked: No audio files found!" in out


def test_a_folder_that_cannot_be_scanned_for_logs_aborts_the_upload(tmp_path, monkeypatch) -> None:
    (tmp_path / "CD2").mkdir()
    real_scandir = os.scandir

    def scandir(path):
        if os.path.basename(path) == "CD2":
            raise PermissionError(13, "Permission denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    with pytest.raises(click.Abort):
        anyio.run(uploader._check_logs, _album_with_a_log(tmp_path))
