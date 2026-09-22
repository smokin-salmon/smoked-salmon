from typing import Any

import anyio
import msgspec
import pytest

import salmon.uploader
from salmon.config.validations import Cfg, ImageUploader

SHARED_HOST_ERROR = r"can only be set as cover_uploader under \[image\.red\] or \[image\.ops\]"


def _image(**settings: Any) -> ImageUploader:
    return msgspec.convert(settings, ImageUploader)


def test_trackers_without_an_override_use_the_global_cover_host() -> None:
    image = _image(cover_uploader="imgbox")
    assert image.cover_uploader_for("RED") == "imgbox"
    assert image.cover_uploader_for("OPS") == "imgbox"


def test_ops_may_also_use_the_red_image_host() -> None:
    image = _image(cover_uploader="imgbox", ops={"cover_uploader": "red"})
    assert image.cover_uploader_for("OPS") == "red"
    assert image.cover_uploader_for("RED") == "imgbox"


def test_a_tracker_override_only_applies_to_that_tracker() -> None:
    image = _image(cover_uploader="imgbox", red={"cover_uploader": "red"})
    assert image.cover_uploader_for("RED") == "red"
    assert image.cover_uploader_for("OPS") == "imgbox"
    assert image.cover_uploader_for("DIC") == "imgbox"


@pytest.mark.parametrize(
    "settings",
    [
        {"cover_uploader": "red"},
        {"image_uploader": "red"},
        {"specs_uploader": "red"},
        {"dic": {"cover_uploader": "red"}},
    ],
)
def test_red_image_host_is_refused_where_other_trackers_would_see_it(settings: dict[str, Any]) -> None:
    with pytest.raises(msgspec.ValidationError, match=SHARED_HOST_ERROR):
        _image(**settings)


def test_a_host_set_only_in_an_override_still_needs_its_key() -> None:
    with pytest.raises(msgspec.ValidationError, match="ptpimg key not specified"):
        _image(ops={"cover_uploader": "ptpimg"})


def _cfg(tmp_path, code: str, red: dict[str, Any]) -> dict[str, Any]:
    return {
        "directory": {"dottorrents_dir": str(tmp_path), "download_directory": str(tmp_path)},
        "image": {code: {"cover_uploader": "red"}},
        "tracker": {"red": red, "ops": {"session": "cookie"}},
    }


@pytest.mark.parametrize("code", ["red", "ops"])
def test_red_cover_host_needs_the_red_api_key(tmp_path, code: str) -> None:
    # The RED key authenticates the upload even when the cover is for OPS.
    with pytest.raises(msgspec.ValidationError, match=f"image.{code}.cover_uploader .* needs tracker.red.api_key"):
        msgspec.convert(_cfg(tmp_path, code, {"session": "cookie"}), Cfg)
    cfg = msgspec.convert(_cfg(tmp_path, code, {"session": "cookie", "api_key": "key"}), Cfg)
    assert cfg.image.cover_uploader_for(code.upper()) == "red"


def test_each_cover_host_gets_its_own_upload_reused_across_trackers(monkeypatch) -> None:
    uploads: list[str | None] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        return "cover.jpg", False

    async def fake_upload(cover_path: str | None, host: str | None = None) -> str | None:
        uploads.append(host)
        # The first RED upload fails, so the next RED upload must retry it.
        if host == "red" and uploads.count("red") == 1:
            return None
        return f"https://{host}/cover.jpg"

    monkeypatch.setattr(salmon.uploader.cfg, "image", _image(cover_uploader="imgbox", red={"cover_uploader": "red"}))
    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)
    monkeypatch.setattr(salmon.uploader, "upload_cover", fake_upload)

    async def run() -> list[str | None]:
        cover_urls: dict[str, str | None] = {}
        return [
            await salmon.uploader.get_cover_url(tracker, cover_urls, "/release", None, False)
            for tracker in ("OPS", "RED", "DIC", "RED")
        ]

    assert anyio.run(run) == [
        "https://imgbox/cover.jpg",
        None,
        "https://imgbox/cover.jpg",
        "https://red/cover.jpg",
    ]
    assert uploads == ["imgbox", "red", "red"]
