"""Answering "skip" to the retry question goes on without the spectrals that failed to upload (#671).

The real catbox and ptscreens uploaders run against the local fake hosts of test_image_upload_connections.
"""

from pathlib import Path

import pytest
from test_image_upload_connections import (  # pyright: ignore[reportMissingImports]
    FakeHost,
    _run,
    _spectrals,
)

import salmon.images as images
from salmon.uploader.upload import generate_t_description


def _description(spectrals: list[tuple[int, str, tuple[str, str]]], urls: dict[int, list[str]]) -> str:
    return generate_t_description(
        {"date": None, "urls": []},
        {"01. One.flac": {"duration": 60, "bit rate": 0, "precision": 16, "sample rate": 44100}},
        False,
        [],
        urls,
        {sid: filename for sid, filename, _ in spectrals},
        None,
        None,
    )


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Answer the retry question with each answer in turn; returns the questions asked."""
    asked: list[str] = []

    async def prompt(message: str, **kwargs: object) -> str:
        asked.append(message)
        return answers[len(asked) - 1]

    monkeypatch.setattr(images.click, "prompt", prompt)
    return asked


def test_a_failed_spectral_can_be_skipped_and_the_upload_goes_on_without_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spectrals = _spectrals(tmp_path, tracks=3)
    asked = _answers(monkeypatch, "skip")

    async def body(catbox: FakeHost, ptscreens: FakeHost) -> None:
        printed: list[str] = []
        monkeypatch.setattr(images.click, "secho", lambda message="", **_kwargs: printed.append(message))
        # The last track, so that none is left unstarted when it fails.
        catbox.failing = {"03 Full.png": 0.0}
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])

        assert len(asked) == 1 and "skip" in asked[0]
        assert sorted(result) == [1, 2]
        # A skip sends nothing more, to the failed host or another.
        assert ptscreens.received == []
        assert [line for line in printed if "Going on without" in line] == [
            "Going on without the spectrals of: track3.flac."
        ]
        description = _description(spectrals, result)
        assert description.count("[img=https://img.example") == 4
        assert "track3.flac" not in description
        assert "[img=]" not in description

    _run(monkeypatch, body)


def test_every_spectral_skipped_leaves_no_spectrals_section_in_the_description(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spectrals = _spectrals(tmp_path, tracks=2)
    _answers(monkeypatch, "skip")

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.failing = {"01 Full.png": 0.0, "02 Full.png": 0.0}
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])

        assert result == {}
        assert "Spectrals" not in _description(spectrals, result)

    _run(monkeypatch, body)


def test_answering_another_host_still_retries_the_failed_spectrals_on_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spectrals = _spectrals(tmp_path, tracks=3)
    _answers(monkeypatch, "ptscreens")

    async def body(catbox: FakeHost, ptscreens: FakeHost) -> None:
        catbox.failing = {"03 Full.png": 0.0}
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])

        assert sorted(result) == [1, 2, 3]
        assert sorted(ptscreens.received) == ["03 Full.png", "03 Zoom.png"]

    _run(monkeypatch, body)
