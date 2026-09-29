"""Downconversion uploads: a converted folder that cannot be read is skipped, the rest go on."""

import contextlib
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest

import salmon.trackers
import salmon.uploader as uploader

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi
    from salmon.uploader.seedbox import UploadManager

SOURCE_NAMES = ("01. Track.flac", "02. Track.flac")


def _track_data() -> dict[str, Any]:
    return {
        name: {
            "channels": 2,
            "sample rate": 192000,
            "bit rate": 4_000_000,
            "precision": 24,
            "duration": 200,
            "t": SimpleNamespace(discnumber="1/1", tracknumber=str(i), artist=["Artist"], title="Track"),
        }
        for i, name in enumerate(SOURCE_NAMES, 1)
    }


def _metadata() -> dict[str, Any]:
    return {"format": "FLAC", "encoding": "24bit Lossless", "encoding_vbr": False, "scene": False, "cover": None}


def _write_flac(path: Path, sample_rate: int, bits: int) -> None:
    """Write a FLAC with only a STREAMINFO block, enough for its audio info to be read."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((sample_rate << 44) | (1 << 41) | ((bits - 1) << 36) | sample_rate * 200).to_bytes(8, "big")
    streaminfo += bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)


def _unreadable_flac(folder: Path) -> None:
    for name in SOURCE_NAMES:
        _write_flac(folder / name, 96000, 24)
    (folder / SOURCE_NAMES[1]).write_bytes(b"not a flac file" * 10)


def _no_audio_files(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "cover.jpg").write_bytes(b"jpeg")


def _fake_conversion(monkeypatch, tmp_path: Path, broken) -> list[tuple[str, int]]:
    """Make the 24-bit conversion a reused, broken folder and the 16-bit one a good folder.

    Returns the (folder, bit depth) of each converted upload, in order.
    """

    async def convert_folder(path, bit_depth, sample_rate, output_dir):
        folder = tmp_path / f"converted {bit_depth}"
        if bit_depth == 24:
            broken(folder)
        else:
            for name in SOURCE_NAMES:
                _write_flac(folder / name, sample_rate, bit_depth)
        return sample_rate, str(folder)

    async def check_folder_structure(path, scene):
        pass

    uploads: list[tuple[str, int]] = []

    async def upload_and_report(gazelle_site, path, group_id, metadata, cover_url, track_data, *args, **kwargs):
        [precision] = {track["precision"] for track in track_data.values()}
        uploads.append((path, precision))
        seedbox_uploader = args[7]
        seedbox_uploader.add_upload_task(path, task_type="folder", is_flac=True)
        return 1, 5, "", None, f"https://tracker.test/torrents.php?torrentid={len(uploads)}"

    monkeypatch.setattr(uploader, "convert_folder", convert_folder)
    monkeypatch.setattr(uploader, "check_folder_structure", check_folder_structure)
    monkeypatch.setattr(uploader, "upload_and_report", upload_and_report)
    return uploads


class FakeUploadManager:
    def __init__(self) -> None:
        self.queued: list[str] = []
        self.executed: list[str] | None = None

    def add_upload_task(self, path: str, **_kwargs: Any) -> None:
        self.queued.append(path)

    async def execute_upload(self) -> None:
        self.executed = list(self.queued)


def _downconversion_tasks() -> list[dict[str, Any]]:
    return [
        option
        for option in uploader.get_downconversion_options(_metadata(), _track_data())
        if option["action"] == "downconvert"
    ]


@pytest.mark.parametrize(
    ("broken", "reason"), [(_unreadable_flac, "not a valid FLAC file"), (_no_audio_files, "No audio files found")]
)
def test_unreadable_converted_folder_is_skipped_and_the_next_task_runs(
    monkeypatch, capsys, tmp_path: Path, broken, reason: str
) -> None:
    uploads = _fake_conversion(monkeypatch, tmp_path, broken)

    _run_downconversions()

    assert uploads == [(str(tmp_path / "converted 16"), 16)]
    out = capsys.readouterr().out
    assert f"Could not read {tmp_path / 'converted 24'}" in out
    assert reason in out
    assert "not uploading it" in out


def _files_in(bits: int, rate: int):
    def write(folder: Path) -> None:
        for name in SOURCE_NAMES:
            _write_flac(folder / name, rate, bits)

    return write


@pytest.mark.parametrize(
    ("broken", "found"),
    [(_files_in(16, 96000), "16 bit 96 kHz"), (_files_in(24, 48000), "24 bit 48 kHz")],
    ids=["16-bit files", "48 kHz files"],
)
def test_converted_folder_in_another_format_is_skipped_and_the_next_task_runs(
    monkeypatch, capsys, tmp_path: Path, broken, found: str
) -> None:
    # A folder already there under the name of the 24-bit 96 kHz conversion, with the right file names.
    uploads = _fake_conversion(monkeypatch, tmp_path, broken)

    _run_downconversions()

    assert uploads == [(str(tmp_path / "converted 16"), 16)]
    out = capsys.readouterr().out
    assert f"{tmp_path / 'converted 24'} holds {found} files, not 24 bit 96 kHz: not uploading it." in out


def _run_downconversions() -> None:
    """Run the 24-bit 96 kHz and the 16-bit 48 kHz downconversions of the 24-bit 192 kHz source."""
    tasks = _downconversion_tasks()
    assert [(task["target_bitdepth"], task["target_sample_rate"]) for task in tasks] == [(24, 96000), (16, 48000)]

    anyio.run(
        uploader.execute_downconversion_tasks,
        tasks,
        "/release",
        cast("BaseGazelleApi", cast("object", SimpleNamespace(site_string="RED"))),
        5,
        _metadata(),
        None,
        _track_data(),
        False,
        False,
        None,
        None,
        None,
        None,
        None,
        cast("UploadManager", cast("object", FakeUploadManager())),
        "WEB",
        "https://tracker.test/torrents.php?torrentid=1",
    )


@contextlib.contextmanager
def _staged_as_is(path: str, scratch: bool):
    yield path, None


def _returning(result: Any = None):
    def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _returning_async(result: Any = None):
    async def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def test_unreadable_converted_folder_leaves_the_main_upload_seeded(monkeypatch, capsys, tmp_path: Path) -> None:
    """The upload flow goes on past the broken conversion, and the seedbox step gets every upload that was made."""
    read_audio_info = uploader.gather_audio_info
    uploads = _fake_conversion(monkeypatch, tmp_path, _unreadable_flac)
    conversion_upload = uploader.upload_and_report

    async def main_upload_then_conversions(gazelle_site, path, *args, **kwargs):
        if path == "/release":
            args[11].add_upload_task(path, task_type="folder", is_flac=True)
            return 1, 5, "", None, "https://tracker.test/torrents.php?torrentid=1"
        return await conversion_upload(gazelle_site, path, *args, **kwargs)

    def gather_audio_info(path, *args, **kwargs):
        # The release itself is stubbed; the converted folders are read for real.
        return {} if path == "/release" else read_audio_info(path, *args, **kwargs)

    manager = FakeUploadManager()
    metadata = {**_metadata(), "artists": [("Artist", "main")], "title": "Album", "catno": "CAT1"}
    for name, fake in {
        "staged_source": _staged_as_is,
        "gather_audio_info": gather_audio_info,
        "check_hybrid": _returning(False),
        "standardize_tags": _returning(),
        "gather_tags": _returning({}),
        "construct_rls_data": _returning(metadata),
        "mqa_test": _returning_async(),
        "upload_upconvert_test": _returning_async(),
        "check_spectrals": _returning_async((False, None)),
        "get_metadata": _returning_async((metadata, None)),
        "edit_metadata": _returning_async(("/release", metadata, {}, {})),
        "concat_track_data": _returning(_track_data()),
        "check_embedded_pictures": _returning(),
        "get_spectrals_path": _returning("/spectrals"),
        "handle_spectrals_upload_and_deletion": _returning_async(),
        "resolve_cover_url": _returning_async((True, None)),
        "UploadManager": lambda: manager,
        "check_requests": _returning_async(None),
        "upload_and_report": main_upload_then_conversions,
        "print_torrents": _returning_async(),
    }.items():
        monkeypatch.setattr(uploader, name, fake)
    monkeypatch.setattr(uploader.cfg.upload, "yes_all", False)
    monkeypatch.setattr(uploader.cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(uploader.cfg.image, "auto_compress_cover", False)
    monkeypatch.setattr(uploader.click, "confirm", _returning(True))
    monkeypatch.setattr(uploader.click, "prompt", _returning_async("1 2"))
    monkeypatch.setattr(salmon.trackers, "choose_tracker", _returning_async(None))
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    site = SimpleNamespace(site_code="RED", site_string="RED", base_url="https://tracker.test")

    anyio.run(lambda: uploader.upload(site, "/release", 5, "WEB", None, (), None))  # type: ignore[arg-type]

    assert uploads == [(str(tmp_path / "converted 16"), 16)]
    assert manager.executed == ["/release", str(tmp_path / "converted 16")]
    out = capsys.readouterr().out
    assert f"Could not read {tmp_path / 'converted 24'}" in out
