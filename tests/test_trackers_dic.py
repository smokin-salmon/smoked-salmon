"""DICMusic's upload form data: the sample rate it requires for 24bit Lossless (#421)."""

import struct
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest
from aiohttp import web

import salmon.uploader as uploader
from salmon import cfg
from salmon.errors import RequestError, UploadRefusedError
from salmon.trackers.dic import DICApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader.upload import compile_data_existing_group, compile_data_new_group, prepare_and_upload

if TYPE_CHECKING:
    from salmon.uploader.seedbox import UploadManager


@pytest.fixture(autouse=True)
def _no_upc_catno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", False)


def _metadata(encoding: str = "24bit Lossless", format: str = "FLAC") -> dict[str, Any]:
    return {
        "title": "Test Album",
        "artists": [("Main Artist", "main")],
        "group_year": 2020,
        "label": "Test Label",
        "catno": None,
        "rls_type": "Album",
        "year": 2020,
        "edition_title": None,
        "format": format,
        "encoding": encoding,
        "encoding_vbr": False,
        "source": "WEB",
        "scene": False,
        "tags": "electronic",
        "comment": None,
        "urls": [],
        "date": None,
    }


def _track_data(*rates: int, precision: int | None = 24) -> dict[str, Any]:
    return {
        f"{i:02d}. Track.flac": {
            "channels": 2,
            "sample rate": rate,
            "bit rate": 2_000_000,
            "precision": precision,
            "duration": 200,
            "t": SimpleNamespace(discnumber="1/1", tracknumber=str(i), artist=["Main Artist"], title="Track"),
        }
        for i, rate in enumerate(rates, 1)
    }


def _new_group_data(site, metadata: dict[str, Any], track_data: dict[str, Any]) -> dict[str, Any]:
    return compile_data_new_group(site, "/release", metadata, track_data, False, None, None, None, None)


def _existing_group_data(site, metadata: dict[str, Any], track_data: dict[str, Any]) -> dict[str, Any]:
    return compile_data_existing_group(site, "/release", 123, metadata, track_data, False, None, None, None, None)


@pytest.mark.parametrize(
    ("rate", "value"),
    [
        (44100, "44.1kHz"),
        (48000, "48kHz"),
        (88200, "88.2kHz"),
        (96000, "96kHz"),
        (176400, "176.4kHz"),
        (192000, "192kHz"),
    ],
)
def test_24bit_upload_carries_the_sample_rate_option(rate: int, value: str) -> None:
    data = _new_group_data(DICApi(), _metadata(), _track_data(rate, rate))

    assert data["sample_rate"] == value


def test_upload_into_an_existing_group_carries_the_sample_rate() -> None:
    data = _existing_group_data(DICApi(), _metadata(), _track_data(96000, 96000))

    assert data["groupid"] == 123
    assert data["sample_rate"] == "96kHz"


@pytest.mark.parametrize(
    ("encoding", "format", "precision"),
    [("Lossless", "FLAC", 16), ("320", "MP3", None), ("V0 (VBR)", "MP3", None)],
)
@pytest.mark.parametrize("compile_data", [_new_group_data, _existing_group_data])
def test_16bit_and_mp3_uploads_carry_no_sample_rate(compile_data, encoding: str, format: str, precision) -> None:
    data = compile_data(DICApi(), _metadata(encoding, format), _track_data(44100, 44100, precision=precision))

    assert "sample_rate" not in data


@pytest.mark.parametrize("site", [RedApi, OpsApi])
@pytest.mark.parametrize("compile_data", [_new_group_data, _existing_group_data])
def test_red_and_ops_uploads_carry_no_sample_rate(compile_data, site) -> None:
    data = compile_data(site(), _metadata(), _track_data(96000, 96000))

    assert "sample_rate" not in data


async def _refused_before_any_request(track_data: dict[str, Any]) -> tuple[UploadRefusedError, int]:
    requests = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        return web.json_response({"status": "failure"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    api = DICApi()
    api.base_url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    api.cookie = "fake-cookie"
    api.api_key = ""
    try:
        with pytest.raises(UploadRefusedError) as refused:
            await prepare_and_upload(
                api, "/release", 123, _metadata(), None, track_data, False, False, None, None, None, None
            )
        return refused.value, requests
    finally:
        await api.close()
        await runner.cleanup()


def test_mixed_sample_rates_stop_the_upload_before_any_request() -> None:
    error, requests = anyio.run(_refused_before_any_request, _track_data(44100, 96000))

    assert requests == 0
    assert "44.1 kHz, 96 kHz" in str(error)
    assert "one sample rate per torrent" in str(error)


def test_unlisted_sample_rate_stops_the_upload_before_any_request() -> None:
    error, requests = anyio.run(_refused_before_any_request, _track_data(352800, 352800))

    assert requests == 0
    assert "352.8 kHz" in str(error)
    assert "no sample rate option" in str(error)


def test_a_refused_upload_only_stops_that_tracker() -> None:
    # The tracker loop in _upload_staged catches RequestError per tracker and offers the next one.
    assert issubclass(UploadRefusedError, RequestError)


def _write_flac(path: Path, sample_rate: int, bits: int = 24) -> None:
    """Write a FLAC with only a STREAMINFO block, enough for its audio info to be read."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((sample_rate << 44) | (1 << 41) | ((bits - 1) << 36) | sample_rate * 10).to_bytes(8, "big")
    streaminfo += bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)


def _downconvert_24bit(monkeypatch, tmp_path: Path, converted_names) -> list[dict[str, Any]]:
    """Run the 24-bit downconversion of a 192 kHz source to DIC; give the upload data it sent.

    The fake conversion writes the given file names into the output folder.
    """
    source_track_data = _track_data(192000, 192000)
    converted = tmp_path / "converted"

    async def convert_folder(path, bit_depth, sample_rate, output_dir):
        for name in converted_names(source_track_data):
            _write_flac(converted / name, sample_rate)
        return sample_rate, str(converted)

    async def check_folder_structure(path, scene):
        pass

    uploads: list[dict[str, Any]] = []

    async def upload_and_report(gazelle_site, path, group_id, metadata, cover_url, track_data, *args, **kwargs):
        uploads.append(_existing_group_data(gazelle_site, metadata, track_data))
        return 1, group_id, "", None, ""

    monkeypatch.setattr(uploader, "convert_folder", convert_folder)
    monkeypatch.setattr(uploader, "check_folder_structure", check_folder_structure)
    monkeypatch.setattr(uploader, "upload_and_report", upload_and_report)
    [task] = [
        option
        for option in uploader.get_downconversion_options(_metadata(), source_track_data)
        if option["action"] == "downconvert" and option["target_bitdepth"] == 24
    ]

    anyio.run(
        uploader.execute_downconversion_tasks,
        [task],
        "/release",
        DICApi(),
        123,
        _metadata(),
        None,
        source_track_data,
        False,
        False,
        None,
        None,
        None,
        None,
        None,
        cast("UploadManager", cast("object", None)),
        None,
        "https://dicmusic.com/torrents.php?torrentid=1",
    )
    return uploads


def test_downconversion_carries_the_rate_of_the_converted_files(monkeypatch, tmp_path: Path) -> None:
    [data] = _downconvert_24bit(monkeypatch, tmp_path, lambda source: list(source))

    assert data["bitrate"] == "24bit Lossless"
    assert data["sample_rate"] == "96kHz"


def test_converted_folder_with_other_files_is_not_uploaded(monkeypatch, capsys, tmp_path: Path) -> None:
    # An output folder that was already there, holding files of other names: nothing tells its rate.
    uploads = _downconvert_24bit(monkeypatch, tmp_path, lambda source: ["01. Other.flac", "02. Other.flac"])

    assert uploads == []
    out = capsys.readouterr().out
    assert "does not hold the same audio files as the source: not uploading it" in out
