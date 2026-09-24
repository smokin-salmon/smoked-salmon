import re
from types import SimpleNamespace
from typing import Any

import anyio
import msgspec
import pytest

import salmon.images as images
from salmon.config import validations
from salmon.config.validations import ImageUploader

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _offered_hosts(prompt_message: str) -> list[str]:
    """Extract the host names listed after 'Options:' in a (possibly styled) prompt message."""
    plain = _ANSI_RE.sub("", prompt_message)
    options = plain.split("Options:", 1)[1]
    options = options.rsplit(")", 1)[0]
    return [host.strip() for host in options.split(",")]


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


def _capture_prompt(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> tuple[list[str], list[str]]:
    """Patch click.prompt/secho and return (prompt messages seen, secho messages printed)."""
    prompt_messages: list[str] = []
    printed: list[str] = []
    answer_iter = iter(answers)

    async def fake_prompt(message: str = "", **kwargs: object) -> str:
        prompt_messages.append(message)
        return next(answer_iter)

    def fake_secho(message: str = "", **kwargs: object) -> None:
        printed.append(message)

    monkeypatch.setattr(images.click, "prompt", fake_prompt)
    monkeypatch.setattr(images.click, "secho", fake_secho)
    return prompt_messages, printed


def test_retry_prompt_refuses_red_and_ra_then_uploads_with_an_allowed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    red_calls: list[str] = []
    ra_calls: list[str] = []
    allowed_calls: list[str] = []
    # Real HOSTS dict, real forbidden-hosts logic: only red, ra and the allowed host's modules
    # are swapped for fakes, so no network happens and we can see which one is ever called.
    monkeypatch.setitem(images.HOSTS, "red", _fake_host_module(red_calls))
    monkeypatch.setitem(images.HOSTS, "ra", _fake_host_module(ra_calls))
    monkeypatch.setitem(images.HOSTS, "catbox", _fake_host_module(allowed_calls))

    prompt_messages, printed = _capture_prompt(monkeypatch, ["red", "ra", "catbox"])

    spectrals = [(1, "track1.flac", ["spec1.png"])]

    async def run() -> dict:
        return await images._handle_failed_spectrals(spectrals, set())

    result = anyio.run(run)

    assert result == {1: ["https://fake/spec1.png"]}
    assert red_calls == []
    assert ra_calls == []
    assert allowed_calls == ["spec1.png"]

    reasons = validations.specs_forbidden_hosts()
    assert any(reasons["red"] in message for message in printed)
    assert any(reasons["ra"] in message for message in printed)

    # The Options list offered by the prompt never includes red or ra.
    assert prompt_messages
    for message in prompt_messages:
        assert "Options:" in message
        offered = _offered_hosts(message)
        assert "red" not in offered
        assert "ra" not in offered


def test_retry_prompt_unknown_host_keeps_the_existing_message(monkeypatch: pytest.MonkeyPatch) -> None:
    allowed_calls: list[str] = []
    monkeypatch.setitem(images.HOSTS, "catbox", _fake_host_module(allowed_calls))

    _prompt_messages, printed = _capture_prompt(monkeypatch, ["notahost", "catbox"])

    spectrals = [(1, "track1.flac", ["spec1.png"])]

    async def run() -> dict:
        return await images._handle_failed_spectrals(spectrals, set())

    result = anyio.run(run)

    assert result == {1: ["https://fake/spec1.png"]}
    assert any("notahost is an invalid image host" in message for message in printed)


def test_retry_prompt_refuses_a_host_newly_added_to_tracker_only_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate someone adding a new tracker-only host to _TRACKER_ONLY_HOSTS: reuse imgbox,
    # which is otherwise a plain allowed host, so no other test is affected.
    monkeypatch.setattr(validations, "_TRACKER_ONLY_HOSTS", {"imgbox": ("dic",)})

    catbox_calls: list[str] = []
    imgbox_calls: list[str] = []
    monkeypatch.setitem(images.HOSTS, "catbox", _fake_host_module(catbox_calls))
    monkeypatch.setitem(images.HOSTS, "imgbox", _fake_host_module(imgbox_calls))

    prompt_messages, printed = _capture_prompt(monkeypatch, ["imgbox", "catbox"])

    spectrals = [(1, "track1.flac", ["spec1.png"])]

    async def run() -> dict:
        return await images._handle_failed_spectrals(spectrals, set())

    result = anyio.run(run)

    assert result == {1: ["https://fake/spec1.png"]}
    assert imgbox_calls == []
    assert catbox_calls == ["spec1.png"]
    assert any("its images only display on DIC" in message for message in printed)
    for message in prompt_messages:
        offered = _offered_hosts(message)
        assert "imgbox" not in offered

    # Config validation refuses the same host for the same reason.
    with pytest.raises(msgspec.ValidationError, match=r"can only be set as cover_uploader under \[image\.dic\]"):
        msgspec.convert({"specs_uploader": "imgbox"}, ImageUploader)
