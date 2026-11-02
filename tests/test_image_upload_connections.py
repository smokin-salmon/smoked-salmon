"""Batch uploads to an image host share a few reused connections (#475).

The real catbox and ptscreens uploaders run against a local fake host that counts the
connections made to it. Nothing here contacts a real image host.
"""

import contextlib
from collections import Counter
from pathlib import Path
from typing import Any, cast

import aiohttp
import anyio
import pytest
from helpers.image_upload_connections import FakeHost, _run, _spectrals

import salmon.images as images
import salmon.images.base as images_base
from salmon.errors import ImageUploadFailed


def _names(spectrals: list[tuple[int, str, tuple[str, str]]], tracks: range) -> list[str]:
    return sorted(Path(path).name for sid, _, paths in spectrals if sid in tracks for path in paths)


def test_spectrals_share_a_few_connections_and_reuse_them(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 3, raising=False)
    spectrals = _spectrals(tmp_path, tracks=10)

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.delay = 0.05
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])

        assert sorted(result) == list(range(1, 11))
        assert result[4] == [f"https://img.example/user/api.php/04 {kind}.png" for kind in ("Full", "Zoom")]
        # Every image uploaded exactly once.
        assert sorted(catbox.received) == _names(spectrals, range(1, 11))
        assert max(Counter(catbox.received).values()) == 1
        # At most 3 connections, never more than 3 open at once, reused across the 20 images.
        assert catbox.peak <= 3
        assert catbox.opened <= 3

    _run(monkeypatch, body)


def test_a_failure_stops_new_tracks_and_the_rest_go_to_the_retry_host_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    spectrals = _spectrals(tmp_path, tracks=10)
    prompts: list[str] = []

    async def prompt(message: str, **kwargs: object) -> str:
        prompts.append(message)
        return "ptscreens"

    monkeypatch.setattr(images.click, "prompt", prompt)

    async def body(catbox: FakeHost, ptscreens: FakeHost) -> None:
        catbox.delay = 0.1
        catbox.failing = {"01 Full.png": 0.0}
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])

        assert len(prompts) == 1
        # Track 1 was started, so both its images went; no other track was sent to the failing host.
        assert sorted(catbox.received) == _names(spectrals, range(1, 2))
        # Everything not uploaded went to the host picked at the prompt, once.
        assert sorted(ptscreens.received) == _names(spectrals, range(1, 11))
        assert sorted(result) == list(range(1, 11))
        assert all(url.startswith("https://img.example/api/1/upload/") for urls in result.values() for url in urls)

    _run(monkeypatch, body)


def test_a_slow_failure_does_not_hold_up_the_rest_of_the_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    spectrals = _spectrals(tmp_path, tracks=10)

    async def prompt(message: str, **kwargs: object) -> str:
        return "ptscreens"

    monkeypatch.setattr(images.click, "prompt", prompt)

    async def body(catbox: FakeHost, ptscreens: FakeHost) -> None:
        catbox.delay = 0.01
        # Track 2's Full image fails, but only after every other image had time to go.
        catbox.failing = {"02 Full.png": 1.0}
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])

        # The other connection kept taking images from the queue while track 2 was stuck.
        assert sorted(catbox.received) == _names(spectrals, range(1, 11))
        # Only track 2 is left for the retry host, and nothing is sent twice to either host.
        assert sorted(ptscreens.received) == _names(spectrals, range(2, 3))
        assert max(Counter(catbox.received).values()) == 1
        assert sorted(result) == list(range(1, 11))

    _run(monkeypatch, body)


def test_waiting_in_the_queue_does_not_count_against_the_upload_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    # Each upload takes 0.2 s, well within 1 s, but the last ones wait about 1.6 s for a connection.
    monkeypatch.setattr(images_base, "UPLOAD_TIMEOUT", aiohttp.ClientTimeout(total=1, sock_connect=1), raising=False)
    spectrals = _spectrals(tmp_path, tracks=8)

    async def prompt(message: str, **kwargs: object) -> str:
        raise AssertionError("an upload failed")

    monkeypatch.setattr(images.click, "prompt", prompt)

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.delay = 0.2
        result = await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])
        assert sorted(result) == list(range(1, 9))

    _run(monkeypatch, body)


def test_cancelling_a_batch_closes_its_connections(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    spectrals = _spectrals(tmp_path, tracks=4)

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.delay = 10
        with anyio.move_on_after(0.3):
            await images.upload_spectrals(spectrals, uploader=images.HOSTS["catbox"])
        assert len(catbox.received) == 2
        await anyio.sleep(0.1)
        assert catbox.open == 0

    _run(monkeypatch, body)


def test_images_up_shares_the_same_few_connections(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    paths = []
    for index in range(6):
        path = tmp_path / f"image{index}.png"
        path.write_bytes(b"png")
        paths.append(str(path))

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.delay = 0.05
        urls = await images.upload_images(tuple(paths), images.HOSTS["catbox"])

        assert urls == [f"https://img.example/user/api.php/image{index}.png" for index in range(6)]
        assert sorted(catbox.received) == sorted(Path(path).name for path in paths)
        assert catbox.peak <= 2
        assert catbox.opened <= 2

    _run(monkeypatch, body)


def test_images_up_still_fails_when_an_upload_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2, raising=False)
    path = tmp_path / "image.png"
    path.write_bytes(b"png")

    async def body(catbox: FakeHost, _ptscreens: FakeHost) -> None:
        catbox.failing = {"image.png": 0.0}
        with pytest.raises(ImageUploadFailed):
            await images.upload_images((str(path),), images.HOSTS["catbox"])

    _run(monkeypatch, body)


def test_two_uploads_failing_at_once_raise_one_plain_error(tmp_path: Path) -> None:
    class Refusing:
        host = "test"

        @contextlib.asynccontextmanager
        async def connections(self, _limit):
            yield

        async def upload_file(self, filename):
            raise ValueError(f"{filename} is not an image")

    paths = [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")]

    with pytest.raises(ValueError, match="is not an image"):
        anyio.run(images._upload_groups, cast("Any", Refusing()), [paths])
