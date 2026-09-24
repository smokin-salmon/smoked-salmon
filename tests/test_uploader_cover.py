import anyio
import msgspec
import pytest

import salmon.uploader
from salmon.config.validations import ImageUploader


@pytest.fixture(autouse=True)
def _interactive(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", False)


def _covers(monkeypatch, *urls: str | None) -> list[str | None]:
    """Make get_cover_url return urls in turn. Returns what each call returned."""
    returned: list[str | None] = []

    async def fake_get_cover_url(*_args) -> str | None:
        returned.append(urls[len(returned)])
        return returned[-1]

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
    return anyio.run(salmon.uploader.resolve_cover_url, "RED", group_id, {}, "/release", None, False)


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

    async def fake_upload(cover_path: str | None, host: str | None = None) -> str | None:
        uploads.append(host)
        return None if len(uploads) == 2 else f"https://{host}/cover.jpg"

    image = msgspec.convert({"cover_uploader": "imgbox", "red": {"cover_uploader": "red"}}, ImageUploader)
    monkeypatch.setattr(salmon.uploader.cfg, "image", image)
    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)
    monkeypatch.setattr(salmon.uploader, "upload_cover", fake_upload)
    _answers(monkeypatch, "r")

    async def run() -> list[tuple[bool, str | None]]:
        cover_urls: dict[str, str | None] = {}
        return [
            await salmon.uploader.resolve_cover_url(tracker, None, cover_urls, "/release", None, False)
            for tracker in ("OPS", "RED")
        ]

    # OPS gets its cover; the RED upload fails once and is retried, without uploading to imgbox again.
    assert anyio.run(run) == [(True, "https://imgbox/cover.jpg"), (True, "https://red/cover.jpg")]
    assert uploads == ["imgbox", "red", "red"]
