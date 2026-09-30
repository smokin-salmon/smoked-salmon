from typing import Any

import anyio
import msgspec
import pytest

import salmon.uploader
from salmon.config import image_hosts
from salmon.config.image_hosts import cover_refusal
from salmon.config.validations import Cfg, ImageUploader
from salmon.config.validations import ImageKind as Kind

SHARED_HOST_ERROR = r"only displays on RED/OPS, so it can only be set under \[image\.red\] or \[image\.ops\]"


def _image(**settings: Any) -> ImageUploader:
    return msgspec.convert(settings, ImageUploader)


def test_trackers_without_an_override_use_the_global_cover_host() -> None:
    image = _image(cover_uploader="imgbox")
    assert image.host_for("RED", "cover_uploader") == "imgbox"
    assert image.host_for("OPS", "cover_uploader") == "imgbox"


def test_ops_may_also_use_the_red_image_host() -> None:
    image = _image(cover_uploader="imgbox", ops={"cover_uploader": "red"})
    assert image.host_for("OPS", "cover_uploader") == "red"
    assert image.host_for("RED", "cover_uploader") == "imgbox"


def test_a_tracker_override_only_applies_to_that_tracker() -> None:
    image = _image(cover_uploader="imgbox", red={"cover_uploader": "red"})
    assert image.host_for("RED", "cover_uploader") == "red"
    assert image.host_for("OPS", "cover_uploader") == "imgbox"
    assert image.host_for("DIC", "cover_uploader") == "imgbox"


@pytest.mark.parametrize(
    "settings",
    [
        {"cover_uploader": "red"},
        {"image_uploader": "red"},
        {"dic": {"cover_uploader": "red"}},
        {"dic": {"image_uploader": "red"}},
    ],
)
def test_red_image_host_is_refused_where_other_trackers_would_see_it(settings: dict[str, Any]) -> None:
    with pytest.raises(msgspec.ValidationError, match=SHARED_HOST_ERROR):
        _image(**settings)


@pytest.mark.parametrize(
    ("settings", "setting"),
    [
        ({"specs_uploader": "red"}, "specs_uploader"),
        ({"red": {"specs_uploader": "red"}}, "red.specs_uploader"),
        ({"ops": {"specs_uploader": "red"}}, "ops.specs_uploader"),
        ({"dic": {"specs_uploader": "red"}}, "dic.specs_uploader"),
    ],
)
def test_red_image_host_is_refused_for_spectrals_even_for_red_and_ops(settings: dict[str, Any], setting: str) -> None:
    with pytest.raises(msgspec.ValidationError, match=f'image.{setting} = "red": RED\'s rules forbid spectrals'):
        _image(**settings)


@pytest.mark.parametrize("code", ["red", "ops", "dic"])
def test_ra_is_refused_as_a_per_tracker_specs_host(code: str) -> None:
    with pytest.raises(msgspec.ValidationError, match=f"image.{code}.specs_uploader .* asks not to use it"):
        _image(ra_key="key", **{code: {"specs_uploader": "ra"}})


@pytest.mark.parametrize("kind", ["image_uploader", "cover_uploader", "specs_uploader"])
def test_each_kind_of_host_can_be_set_per_tracker(kind: Kind) -> None:
    image = _image(**{kind: "catbox", "ops": {kind: "imgbox"}})
    assert image.host_for("OPS", kind) == "imgbox"
    assert image.host_for("RED", kind) == "catbox"
    assert image.host_for("DIC", kind) == "catbox"
    # Images that are for no tracker in particular use the [image] setting.
    assert image.host_for(None, kind) == "catbox"


@pytest.mark.parametrize("kind", ["image_uploader", "cover_uploader", "specs_uploader"])
def test_a_per_tracker_setting_only_overrides_its_own_kind(kind: Kind) -> None:
    image = _image(red={kind: "imgbox"})
    for other in ("image_uploader", "cover_uploader", "specs_uploader"):
        assert image.host_for("RED", other) == ("imgbox" if other == kind else "catbox")


@pytest.mark.parametrize("code", ["red", "ops"])
def test_red_may_be_a_per_tracker_image_host_where_it_displays(code: str) -> None:
    image = _image(**{code: {"image_uploader": "red"}})
    assert image.host_for(code.upper(), "image_uploader") == "red"
    assert image.host_for("DIC", "image_uploader") == "catbox"


def test_a_host_set_only_as_a_per_tracker_specs_host_still_needs_its_key() -> None:
    with pytest.raises(msgspec.ValidationError, match="imgbb key not specified"):
        _image(dic={"specs_uploader": "imgbb"})


def test_a_tracker_only_host_allowed_for_spectrals_is_accepted_only_for_its_trackers(monkeypatch) -> None:
    # No host is like this today: whether one may host spectrals is decided by HOST_RULES alone.
    monkeypatch.setitem(image_hosts.HOST_RULES, "imgbox", image_hosts.HostRules(displays_on=("ops",)))
    image = _image(ops={"specs_uploader": "imgbox"})
    assert image.host_for("OPS", "specs_uploader") == "imgbox"
    with pytest.raises(msgspec.ValidationError, match="image.red.specs_uploader .* only display on OPS"):
        _image(red={"specs_uploader": "imgbox"})
    with pytest.raises(msgspec.ValidationError, match='image.specs_uploader = "imgbox": its images only display'):
        _image(specs_uploader="imgbox")


def test_a_host_set_only_in_an_override_still_needs_its_key() -> None:
    with pytest.raises(msgspec.ValidationError, match="PTScreens key not specified"):
        _image(ops={"cover_uploader": "ptscreens"})


def test_ra_cover_uploader_needs_its_key() -> None:
    with pytest.raises(msgspec.ValidationError, match="ra key not specified"):
        _image(cover_uploader="ra")


def test_ra_cover_uploader_loads_with_its_key() -> None:
    image = _image(cover_uploader="ra", ra_key="key")
    assert image.cover_uploader == "ra"


def test_ra_specs_uploader_is_refused() -> None:
    with pytest.raises(msgspec.ValidationError, match="Ra's owner asks not to use it for spectrals"):
        _image(specs_uploader="ra", ra_key="key")


def test_ra_may_be_used_as_a_per_tracker_cover_host() -> None:
    image = _image(cover_uploader="imgbox", ra_key="key", red={"cover_uploader": "ra"})
    assert image.host_for("RED", "cover_uploader") == "ra"
    assert image.host_for("OPS", "cover_uploader") == "imgbox"


def _cfg(tmp_path, code: str, red: dict[str, Any], kind: Kind = "cover_uploader") -> dict[str, Any]:
    return {
        "directory": {"dottorrents_dir": str(tmp_path), "download_directory": str(tmp_path)},
        "image": {code: {kind: "red"}},
        "tracker": {"red": red, "ops": {"session": "cookie"}},
    }


@pytest.mark.parametrize("code", ["red", "ops"])
@pytest.mark.parametrize("kind", ["cover_uploader", "image_uploader"])
def test_red_image_host_needs_the_red_api_key(tmp_path, code: str, kind: Kind) -> None:
    # The RED key authenticates the upload even when the image is for OPS.
    with pytest.raises(msgspec.ValidationError, match=f"image.{code}.{kind} .* needs tracker.red.api_key"):
        msgspec.convert(_cfg(tmp_path, code, {"session": "cookie"}, kind), Cfg)
    cfg = msgspec.convert(_cfg(tmp_path, code, {"session": "cookie", "api_key": "key"}, kind), Cfg)
    assert cfg.image.host_for(code.upper(), kind) == "red"


def test_each_cover_host_gets_its_own_upload_reused_across_trackers(monkeypatch) -> None:
    uploads: list[str | None] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        return "cover.jpg", False

    async def fake_upload(cover_path: str | None, host: str | None = None, red_api: object = None) -> str | None:
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
        results = []
        for tracker in ("OPS", "RED", "DIC", "RED"):
            url, _ = await salmon.uploader.get_cover_url(tracker, cover_urls, "/release", None, False)
            results.append(url)
        return results

    assert anyio.run(run) == [
        "https://imgbox/cover.jpg",
        None,
        "https://imgbox/cover.jpg",
        "https://red/cover.jpg",
    ]
    assert uploads == ["imgbox", "red", "red"]


@pytest.mark.parametrize("tracker", ["RED", "OPS", "red", "ops"])
def test_red_is_offered_as_a_cover_host_for_red_and_ops(tracker: str) -> None:
    assert cover_refusal("red", tracker) is None


def test_red_is_not_offered_as_a_cover_host_for_dic() -> None:
    assert cover_refusal("red", "DIC") is not None


def test_a_host_with_no_display_restriction_is_never_refused_as_a_cover_host() -> None:
    assert cover_refusal("catbox", "DIC") is None
