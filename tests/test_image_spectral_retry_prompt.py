import re
from types import SimpleNamespace
from typing import Any, get_args

import anyio
import msgspec
import pytest

import salmon.images as images
from salmon.config.validations import ImageUploader, ImgUploaderLiteral
from salmon.images import rules
from salmon.images.rules import HOST_RULES, HostRules

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


def _run_retry_prompt(spectrals) -> dict:
    async def run() -> dict:
        return await images._handle_failed_spectrals(spectrals, set())

    return anyio.run(run)


def test_retry_prompt_refuses_red_and_ra_then_uploads_with_an_allowed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    red_calls: list[str] = []
    ra_calls: list[str] = []
    allowed_calls: list[str] = []
    # Real HOSTS dict, real HOST_RULES: only red, ra and the allowed host's modules are
    # swapped for fakes, so no network happens and we can see which one is ever called.
    monkeypatch.setitem(images.HOSTS, "red", _fake_host_module(red_calls))
    monkeypatch.setitem(images.HOSTS, "ra", _fake_host_module(ra_calls))
    monkeypatch.setitem(images.HOSTS, "catbox", _fake_host_module(allowed_calls))

    prompt_messages, printed = _capture_prompt(monkeypatch, ["red", "ra", "catbox"])

    result = _run_retry_prompt([(1, "track1.flac", ["spec1.png"])])

    assert result == {1: ["https://fake/spec1.png"]}
    assert red_calls == []
    assert ra_calls == []
    assert allowed_calls == ["spec1.png"]

    red_reason = rules.spectrals_refusal("red")
    ra_reason = rules.spectrals_refusal("ra")
    assert red_reason is not None and any(red_reason in message for message in printed)
    assert ra_reason is not None and any(ra_reason in message for message in printed)

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

    result = _run_retry_prompt([(1, "track1.flac", ["spec1.png"])])

    assert result == {1: ["https://fake/spec1.png"]}
    assert any("notahost is an invalid image host" in message for message in printed)


def test_a_host_newly_added_to_host_rules_is_refused_by_both(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate two kinds of new rows in HOST_RULES: one tracker-only (like red), one refused
    # outright with no display restriction (like ra). Reuse imgbox and imgbb, which are
    # otherwise plain allowed hosts, so no other test is affected.
    monkeypatch.setitem(rules.HOST_RULES, "imgbox", HostRules(displays_on=("dic",)))
    monkeypatch.setitem(rules.HOST_RULES, "imgbb", HostRules(spectrals_refused="a made-up reason"))

    catbox_calls: list[str] = []
    imgbox_calls: list[str] = []
    imgbb_calls: list[str] = []
    monkeypatch.setitem(images.HOSTS, "catbox", _fake_host_module(catbox_calls))
    monkeypatch.setitem(images.HOSTS, "imgbox", _fake_host_module(imgbox_calls))
    monkeypatch.setitem(images.HOSTS, "imgbb", _fake_host_module(imgbb_calls))

    prompt_messages, printed = _capture_prompt(monkeypatch, ["imgbox", "imgbb", "catbox"])

    result = _run_retry_prompt([(1, "track1.flac", ["spec1.png"])])

    assert result == {1: ["https://fake/spec1.png"]}
    assert imgbox_calls == []
    assert imgbb_calls == []
    assert catbox_calls == ["spec1.png"]
    assert any("its images only display on DIC" in message for message in printed)
    assert any("a made-up reason" in message for message in printed)
    for message in prompt_messages:
        offered = _offered_hosts(message)
        assert "imgbox" not in offered
        assert "imgbb" not in offered

    # Config validation refuses the same hosts, for the same reasons.
    with pytest.raises(msgspec.ValidationError, match=r"can only be set as cover_uploader under \[image\.dic\]"):
        msgspec.convert({"specs_uploader": "imgbox"}, ImageUploader)
    with pytest.raises(msgspec.ValidationError, match="a made-up reason"):
        msgspec.convert({"specs_uploader": "imgbb", "imgbb_key": "key"}, ImageUploader)


def test_every_host_rules_key_is_a_valid_image_host() -> None:
    valid_hosts = set(get_args(ImgUploaderLiteral))
    for host in HOST_RULES:
        assert host in images.HOSTS
        assert host in valid_hosts
