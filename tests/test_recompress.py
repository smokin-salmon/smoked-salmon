"""compress() and recompress_path(): failures are reported, and files re-encode in parallel."""

import subprocess

import anyio
import pytest

from salmon import cfg
from salmon.common import files as files_module
from salmon.common.files import CompressResult, compress
from salmon.errors import UploadError
from salmon.tagger import audio_info


def _write_flac(path) -> None:
    path.write_bytes(b"fLaC" + bytes(30))


def test_compress_success_runs_the_expected_command(monkeypatch, tmp_path) -> None:
    seen: list[list[str]] = []

    async def fake_run_process(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(files_module.anyio, "run_process", fake_run_process)

    filepath = str(tmp_path / "01.flac")
    result = anyio.run(compress, filepath)

    assert result == CompressResult(filepath, True)
    assert seen == [
        [
            "flac",
            f"-{cfg.upload.compression.flac_compression_level}",
            "-V",
            filepath,
            "--force",
        ]
    ]


def test_compress_reports_flac_failure(monkeypatch, tmp_path) -> None:
    async def fake_run_process(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"track.flac: ERROR, failed to decode\n")

    monkeypatch.setattr(files_module.anyio, "run_process", fake_run_process)

    filepath = str(tmp_path / "01.flac")
    result = anyio.run(compress, filepath)

    assert result.success is False
    assert result.filepath == filepath
    assert result.error is not None
    assert "failed to decode" in result.error


def test_compress_reports_missing_flac_binary(monkeypatch, tmp_path) -> None:
    async def fake_run_process(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("[Errno 2] No such file or directory: 'flac'")

    monkeypatch.setattr(files_module.anyio, "run_process", fake_run_process)

    filepath = str(tmp_path / "01.flac")
    result = anyio.run(compress, filepath)

    assert result.success is False
    assert result.error is not None
    assert "flac" in result.error


def test_recompress_path_reports_a_single_failure_and_still_processes_the_rest(monkeypatch, tmp_path) -> None:
    filenames = ["01.flac", "02.flac", "03.flac"]
    for name in filenames:
        _write_flac(tmp_path / name)

    processed: list[str] = []

    async def fake_compress(filepath: str) -> CompressResult:
        processed.append(filepath)
        if filepath.endswith("02.flac"):
            return CompressResult(filepath, False, "ERROR, failed to decode")
        return CompressResult(filepath, True)

    monkeypatch.setattr(audio_info, "compress", fake_compress)

    with pytest.raises(UploadError):
        anyio.run(audio_info.recompress_path, str(tmp_path))

    assert sorted(processed) == sorted(str(tmp_path / name) for name in filenames)


def test_recompress_path_reports_missing_flac_binary(monkeypatch, tmp_path) -> None:
    _write_flac(tmp_path / "01.flac")

    async def fake_run_process(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("[Errno 2] No such file or directory: 'flac'")

    monkeypatch.setattr(files_module.anyio, "run_process", fake_run_process)

    with pytest.raises(UploadError, match="1 file"):
        anyio.run(audio_info.recompress_path, str(tmp_path))


def test_recompress_path_succeeds_when_all_files_succeed(monkeypatch, tmp_path) -> None:
    filenames = ["01.flac", "02.flac"]
    for name in filenames:
        _write_flac(tmp_path / name)

    processed: list[str] = []

    async def fake_compress(filepath: str) -> CompressResult:
        processed.append(filepath)
        return CompressResult(filepath, True)

    monkeypatch.setattr(audio_info, "compress", fake_compress)

    anyio.run(audio_info.recompress_path, str(tmp_path))

    assert sorted(processed) == sorted(str(tmp_path / name) for name in filenames)


def test_recompress_path_caps_concurrency_at_simultaneous_threads(monkeypatch, tmp_path) -> None:
    filenames = ["01.flac", "02.flac", "03.flac", "04.flac"]
    for name in filenames:
        _write_flac(tmp_path / name)

    monkeypatch.setattr(cfg.upload, "simultaneous_threads", 2)

    current = 0
    peak = 0

    async def fake_compress(filepath: str) -> CompressResult:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        await anyio.sleep(0.05)
        current -= 1
        return CompressResult(filepath, True)

    monkeypatch.setattr(audio_info, "compress", fake_compress)

    anyio.run(audio_info.recompress_path, str(tmp_path))

    assert peak == 2
