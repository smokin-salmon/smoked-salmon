from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import salmon.images as images


class _FakeUploader:
    """A fake ImageUploader module that records the files it was asked to upload."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def ImageUploader(self) -> Any:  # noqa: N802 - mirrors the real modules' API
        calls = self._calls

        class _Instance:
            async def upload_file(self, filename: str) -> tuple[str, None]:
                calls.append(filename)
                return f"https://fake/{filename}", None

        return _Instance()


def _fake_host_module(calls: list[str]) -> Any:
    return SimpleNamespace(ImageUploader=_FakeUploader(calls).ImageUploader)


def test_retry_prompt_refuses_red_and_ra_then_uploads_with_an_allowed_host(monkeypatch) -> None:
    allowed_calls: list[str] = []
    monkeypatch.setattr(images, "HOSTS", {"allowed": _fake_host_module(allowed_calls), "red": object(), "ra": object()})
    monkeypatch.setattr(images, "SPECS_FORBIDDEN_HOSTS", {"red": "reason for red", "ra": "reason for ra"})
    monkeypatch.setattr(images, "SPECS_ALLOWED_HOSTS", ["allowed"])

    answers = iter(["red", "ra", "allowed"])
    printed: list[str] = []

    async def fake_prompt(*args: object, **kwargs: object) -> str:
        return next(answers)

    def fake_secho(message: str = "", **kwargs: object) -> None:
        printed.append(message)

    monkeypatch.setattr(images.click, "prompt", fake_prompt)
    monkeypatch.setattr(images.click, "secho", fake_secho)

    spectrals = [(1, "track1.flac", ["spec1.png"])]

    async def run() -> dict:
        return await images._handle_failed_spectrals(spectrals, set())

    result = anyio.run(run)

    assert result == {1: ["https://fake/spec1.png"]}
    assert allowed_calls == ["spec1.png"]
    assert any("reason for red" in message for message in printed)
    assert any("reason for ra" in message for message in printed)


def test_retry_prompt_options_never_list_red_or_ra() -> None:
    assert "red" not in images.SPECS_ALLOWED_HOSTS
    assert "ra" not in images.SPECS_ALLOWED_HOSTS


def test_retry_prompt_unknown_host_keeps_the_existing_message(monkeypatch) -> None:
    allowed_calls: list[str] = []
    monkeypatch.setattr(images, "HOSTS", {"allowed": _fake_host_module(allowed_calls)})
    monkeypatch.setattr(images, "SPECS_FORBIDDEN_HOSTS", {})
    monkeypatch.setattr(images, "SPECS_ALLOWED_HOSTS", ["allowed"])

    answers = iter(["notahost", "allowed"])
    printed: list[str] = []

    async def fake_prompt(*args: object, **kwargs: object) -> str:
        return next(answers)

    def fake_secho(message: str = "", **kwargs: object) -> None:
        printed.append(message)

    monkeypatch.setattr(images.click, "prompt", fake_prompt)
    monkeypatch.setattr(images.click, "secho", fake_secho)

    spectrals = [(1, "track1.flac", ["spec1.png"])]

    async def run() -> dict:
        return await images._handle_failed_spectrals(spectrals, set())

    result = anyio.run(run)

    assert result == {1: ["https://fake/spec1.png"]}
    assert any("notahost is an invalid image host" in message for message in printed)


@pytest.mark.parametrize("host", ["red", "ra"])
def test_forbidden_hosts_come_from_the_shared_config_mapping(host: str) -> None:
    from salmon.config.validations import SPECS_FORBIDDEN_HOSTS

    assert host in SPECS_FORBIDDEN_HOSTS
    assert host in images.SPECS_FORBIDDEN_HOSTS
