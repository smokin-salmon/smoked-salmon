import anyio
import msgspec
import pytest

import salmon.uploader
from salmon.config.validations import ImageUploader
from salmon.trackers.dic import DICApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi


@pytest.fixture(autouse=True)
def _interactive(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", False)


def _covers(monkeypatch, *urls: str | None) -> list[str | None]:
    """Make get_cover_url return urls in turn, each as if no cover file were found.

    Returns what each call returned.
    """
    returned: list[str | None] = []

    async def fake_get_cover_url(*_args) -> tuple[str | None, bool]:
        returned.append(urls[len(returned)])
        return returned[-1], False

    monkeypatch.setattr(salmon.uploader, "get_cover_url", fake_get_cover_url)
    return returned


def _answers(monkeypatch, *answers: str) -> list[str]:
    """Answer the prompts with answers in turn. Returns the prompts asked."""
    asked: list[str] = []

    async def fake_prompt(text: str, *_args, **_kwargs) -> str:
        asked.append(text)
        return answers[len(asked) - 1]

    monkeypatch.setattr(salmon.uploader.click, "prompt", fake_prompt)
    return asked


def _resolve(group_id: int | None = None) -> tuple[bool, str | None]:
    return anyio.run(salmon.uploader.resolve_cover_url, RedApi(), group_id, {}, "/release", None, False)


def test_yes_all_does_not_upload_a_new_group_without_a_cover(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    _covers(monkeypatch, None)
    asked = _answers(monkeypatch)

    assert _resolve() == (False, None)
    assert asked == []


@pytest.mark.parametrize("answer", ["n", "", "no", "x"])
def test_declining_to_go_on_without_a_cover_stops(monkeypatch, answer: str) -> None:
    _covers(monkeypatch, None)
    asked = _answers(monkeypatch, answer)

    assert _resolve() == (False, None)
    assert len(asked) == 1


@pytest.mark.parametrize("answer", ["y", "Yes "])
def test_accepting_to_go_on_without_a_cover_uploads_without_one(monkeypatch, answer: str) -> None:
    _covers(monkeypatch, None)
    _answers(monkeypatch, answer)

    assert _resolve() == (True, None)


def test_retry_uses_a_cover_that_appeared(monkeypatch) -> None:
    returned = _covers(monkeypatch, None, None, "https://host/cover.jpg")
    asked = _answers(monkeypatch, "r", "r")

    assert _resolve() == (True, "https://host/cover.jpg")
    assert len(returned) == 3
    assert len(asked) == 2


def test_existing_group_needs_no_cover(monkeypatch) -> None:
    returned = _covers(monkeypatch)
    asked = _answers(monkeypatch)
    downloads: list[str | None] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        downloads.append(cover_source)
        return "cover.jpg", True

    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)

    assert _resolve(group_id=123) == (True, None)
    assert returned == []
    assert asked == []
    assert downloads == [None]


def test_available_cover_needs_no_prompt(monkeypatch) -> None:
    returned = _covers(monkeypatch, "https://host/cover.jpg")
    asked = _answers(monkeypatch)

    assert _resolve() == (True, "https://host/cover.jpg")
    assert len(returned) == 1
    assert asked == []


def test_retry_does_not_upload_again_to_a_host_that_has_the_cover(monkeypatch) -> None:
    uploads: list[str | None] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        return "cover.jpg", False

    async def fake_upload(cover_path: str | None, host: str | None = None, red_api: object = None) -> str | None:
        uploads.append(host)
        return None if len(uploads) == 2 else f"https://{host}/cover.jpg"

    image = msgspec.convert({"cover_uploader": "imgbox", "red": {"cover_uploader": "red"}}, ImageUploader)
    monkeypatch.setattr(salmon.uploader.cfg, "image", image)
    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)
    monkeypatch.setattr(salmon.uploader, "upload_cover", fake_upload)
    _answers(monkeypatch, "r", "red")

    async def run() -> list[tuple[bool, str | None]]:
        cover_urls: dict[str, str | None] = {}
        return [
            await salmon.uploader.resolve_cover_url(site, None, cover_urls, "/release", None, False)
            for site in (OpsApi(), RedApi())
        ]

    # OPS gets its cover; the RED upload fails once and is retried, without uploading to imgbox again.
    assert anyio.run(run) == [(True, "https://imgbox/cover.jpg"), (True, "https://red/cover.jpg")]
    assert uploads == ["imgbox", "red", "red"]


def test_retry_after_a_failed_upload_offers_another_host(monkeypatch) -> None:
    uploads: list[str | None] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        return "cover.jpg", False

    async def fake_upload(cover_path: str | None, host: str | None = None, red_api: object = None) -> str | None:
        uploads.append(host)
        return None if host == "catbox" else f"https://{host}/cover.jpg"

    image = msgspec.convert({"cover_uploader": "catbox"}, ImageUploader)
    monkeypatch.setattr(salmon.uploader.cfg, "image", image)
    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)
    monkeypatch.setattr(salmon.uploader, "upload_cover", fake_upload)
    asked = _answers(monkeypatch, "r", "imgbox")

    assert _resolve() == (True, "https://imgbox/cover.jpg")
    assert uploads == ["catbox", "imgbox"]
    assert len(asked) == 2
    assert "Which image host" in asked[1]


def test_invalid_cover_host_answer_is_refused_and_asked_again(monkeypatch) -> None:
    uploads: list[str | None] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        return "cover.jpg", False

    async def fake_upload(cover_path: str | None, host: str | None = None, red_api: object = None) -> str | None:
        uploads.append(host)
        return None if host == "catbox" else f"https://{host}/cover.jpg"

    image = msgspec.convert({"cover_uploader": "catbox"}, ImageUploader)
    monkeypatch.setattr(salmon.uploader.cfg, "image", image)
    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)
    monkeypatch.setattr(salmon.uploader, "upload_cover", fake_upload)
    asked = _answers(monkeypatch, "r", "notahost", "imgbox")

    assert _resolve() == (True, "https://imgbox/cover.jpg")
    assert len(asked) == 3
    assert uploads == ["catbox", "imgbox"]


def test_dic_cover_retry_does_not_offer_red_and_refuses_it_if_typed(monkeypatch) -> None:
    uploads: list[str | None] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        return "cover.jpg", False

    async def fake_upload(cover_path: str | None, host: str | None = None, red_api: object = None) -> str | None:
        uploads.append(host)
        return None if host in ("catbox", "red") else f"https://{host}/cover.jpg"

    image = msgspec.convert({"cover_uploader": "catbox"}, ImageUploader)
    monkeypatch.setattr(salmon.uploader.cfg, "image", image)
    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)
    monkeypatch.setattr(salmon.uploader, "upload_cover", fake_upload)
    asked = _answers(monkeypatch, "r", "red", "imgbox")

    result = anyio.run(salmon.uploader.resolve_cover_url, DICApi(), None, {}, "/release", None, False)

    assert result == (True, "https://imgbox/cover.jpg")
    assert "red" not in uploads
    assert len(asked) == 3
    assert "red" not in asked[1]


def test_retry_to_red_host_for_ops_uses_a_red_client(monkeypatch) -> None:
    seen_red_apis: list[object] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        return "cover.jpg", False

    async def fake_upload(cover_path: str | None, host: str | None = None, red_api: object = None) -> str | None:
        if host == "red":
            seen_red_apis.append(red_api)
        return None if host == "catbox" else f"https://{host}/cover.jpg"

    image = msgspec.convert({"cover_uploader": "catbox"}, ImageUploader)
    monkeypatch.setattr(salmon.uploader.cfg, "image", image)
    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)
    monkeypatch.setattr(salmon.uploader, "upload_cover", fake_upload)
    _answers(monkeypatch, "r", "red")

    result = anyio.run(salmon.uploader.resolve_cover_url, OpsApi(), None, {}, "/release", None, False)

    assert result == (True, "https://red/cover.jpg")
    assert len(seen_red_apis) == 1
    assert isinstance(seen_red_apis[0], RedApi)
