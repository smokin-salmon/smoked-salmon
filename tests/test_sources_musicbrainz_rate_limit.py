"""MusicBrainz asks for at most one request a second from each client (#1065), across every salmon web job."""

import threading
import time
from collections.abc import Iterator

import anyio
import pytest

from salmon.sources import musicbrainz


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every wait the limiter asks for, recorded instead of slept."""
    waits: list[float] = []

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(musicbrainz.anyio, "sleep", _sleep)
    return waits


def _clock(monkeypatch: pytest.MonkeyPatch, readings: Iterator[float]) -> None:
    monkeypatch.setattr(musicbrainz, "monotonic", lambda: next(readings))


async def _wait(limiter: musicbrainz.RateLimiter, times: int) -> None:
    for _ in range(times):
        await limiter.wait()


def test_back_to_back_requests_are_spaced_a_second_apart(monkeypatch: pytest.MonkeyPatch, slept: list[float]) -> None:
    _clock(monkeypatch, iter([100.0] * 3))

    anyio.run(_wait, musicbrainz.RateLimiter(1.0), 3)

    assert slept == [1.0, 2.0]


def test_a_request_after_the_interval_does_not_wait(monkeypatch: pytest.MonkeyPatch, slept: list[float]) -> None:
    _clock(monkeypatch, iter([100.0, 101.5]))

    anyio.run(_wait, musicbrainz.RateLimiter(1.0), 2)

    assert slept == []


def test_two_threads_with_their_own_loops_share_the_limiter() -> None:
    # Under salmon web each job runs in a thread with its own event loop: the limiter must hold across them.
    limiter = musicbrainz.RateLimiter(0.2)
    passed: list[float] = []

    async def _requests() -> None:
        for _ in range(3):
            await limiter.wait()
            passed.append(time.monotonic())

    threads = [threading.Thread(target=anyio.run, args=(_requests,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    times = sorted(passed)
    assert len(times) == 6
    assert all(later - earlier >= 0.19 for earlier, later in zip(times, times[1:], strict=False))
