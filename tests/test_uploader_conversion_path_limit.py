"""The downconversion and transcode folder checks use the run's computed path limit, not the
180 default: an OPS-only run must not truncate a transcode's file names to 180 when its FLAC was
allowed 255."""

from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest

import salmon.uploader as uploader
from salmon import cfg
from salmon.trackers.ops import OpsApi

if TYPE_CHECKING:
    from salmon.uploader.seedbox import UploadManager

NO_SEEDBOX_UPLOADER = cast("UploadManager", cast("object", None))


def _returning_async(result: Any = None):
    async def fake(*_args: Any, **_kwargs: Any) -> Any:
        return result

    return fake


@pytest.fixture
def recorded_checks(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_check_folder_structure(path, scene, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(uploader, "check_folder_structure", fake_check_folder_structure)
    return calls


def _base_setup(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(uploader, "downconversion_format", lambda task: ("MP3", "320"))
    monkeypatch.setattr(uploader, "generate_conversion_description", lambda *a, **k: "desc")
    monkeypatch.setattr(uploader, "generate_transcode_description", lambda *a, **k: "desc")
    monkeypatch.setattr(uploader, "upload_and_report", _returning_async((1, 5, "", None, "https://tracker.test/t")))


def test_downconvert_folder_check_uses_the_runs_path_limit(monkeypatch, recorded_checks, tmp_path) -> None:
    _base_setup(monkeypatch)
    track_data = {"01.flac": {"sample rate": 96000, "precision": 24}}
    converted_path = str(tmp_path)
    monkeypatch.setattr(uploader, "convert_folder", _returning_async((96000, converted_path)))
    monkeypatch.setattr(uploader, "gather_audio_info", lambda *a, **k: track_data)

    task = {"name": "24 bit 96 kHz", "action": "downconvert", "target_bitdepth": 24, "target_sample_rate": 96000}

    anyio.run(
        uploader.execute_downconversion_tasks,
        [task],
        "/release",
        OpsApi(),
        5,
        {"scene": False},
        None,
        track_data,
        False,
        False,
        None,
        None,
        None,
        None,
        None,
        NO_SEEDBOX_UPLOADER,
        "WEB",
        "https://tracker.test/torrents.php?torrentid=1",
    )

    assert recorded_checks == [{"max_path_length": 255}]


def test_transcode_folder_check_uses_the_runs_path_limit(monkeypatch, recorded_checks, tmp_path) -> None:
    _base_setup(monkeypatch)
    monkeypatch.setattr(uploader, "transcode_folder", _returning_async(str(tmp_path)))

    task = {"name": "MP3 320", "action": "transcode", "encoding": "320"}

    anyio.run(
        uploader.execute_downconversion_tasks,
        [task],
        "/release",
        OpsApi(),
        5,
        {"scene": False},
        None,
        {},
        False,
        False,
        None,
        None,
        None,
        None,
        None,
        NO_SEEDBOX_UPLOADER,
        "WEB",
        "https://tracker.test/torrents.php?torrentid=1",
    )

    assert recorded_checks == [{"max_path_length": 255}]
