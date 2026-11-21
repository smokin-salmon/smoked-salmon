"""MusicBrainz asks for at most one request a second from each client (#1065), across every salmon web job."""

import threading
import time

import anyio
import pytest

from salmon.sources import musicbrainz


class _Clock:
    """The limiter's clock, which only a sleep moves on: each sleep is recorded and passes at once."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(musicbrainz, "monotonic", lambda: fake.now)
    monkeypatch.setattr(musicbrainz.anyio, "sleep", fake.sleep)
    return fake


async def _take(limiter: musicbrainz.RateLimiter) -> None:
    async with limiter.turn():
        pass


async def _wait(limiter: musicbrainz.RateLimiter, times: int) -> None:
    for _ in range(times):
        await _take(limiter)


def test_back_to_back_requests_are_spaced_a_second_apart(clock: _Clock) -> None:
    anyio.run(_wait, musicbrainz.RateLimiter(1.0), 3)

    assert clock.slept == [1.0, 1.0]


def test_a_request_after_the_interval_does_not_wait(clock: _Clock) -> None:
    limiter = musicbrainz.RateLimiter(1.0)

    anyio.run(_take, limiter)
    clock.now += 1.5
    anyio.run(_take, limiter)

    assert clock.slept == []


def test_a_request_that_fails_ends_its_turn(clock: _Clock) -> None:
    limiter = musicbrainz.RateLimiter(1.0)

    async def _fails() -> None:
        async with limiter.turn():
            raise ConnectionError

    with pytest.raises(ConnectionError):
        anyio.run(_fails)
    anyio.run(_take, limiter)

    assert clock.slept == [1.0]


def test_a_waiter_that_wakes_late_does_not_go_on_the_next_ones_heels(monkeypatch: pytest.MonkeyPatch) -> None:
    # A salmon web job's thread can be slow to wake. A slot reserved ahead of time would let it go late, right
    # beside the waiter after it: each turn is granted a whole interval after the last one ended instead.
    limiter = musicbrainz.RateLimiter(0.2)
    real_sleep = anyio.sleep
    late: list[None] = []

    async def _sleep(seconds: float) -> None:
        if not late:  # The first waiter to sleep wakes 0.15 s late
            late.append(None)
            seconds += 0.15
        await real_sleep(seconds)

    monkeypatch.setattr(musicbrainz.anyio, "sleep", _sleep)
    granted: list[float] = []

    async def _turn() -> None:
        await _take(limiter)
        granted.append(time.monotonic())

    async def _three() -> None:
        async with anyio.create_task_group() as tasks:
            for _ in range(3):
                tasks.start_soon(_turn)

    anyio.run(_three)

    times = sorted(granted)
    assert len(times) == 3
    assert all(later - earlier >= 0.19 for earlier, later in zip(times, times[1:], strict=False))


def test_two_threads_with_their_own_loops_share_the_limiter() -> None:
    # Under salmon web each job runs in a thread with its own event loop: the limiter must hold across them.
    limiter = musicbrainz.RateLimiter(0.2)
    passed: list[float] = []

    async def _requests() -> None:
        for _ in range(3):
            await _take(limiter)
            passed.append(time.monotonic())

    threads = [threading.Thread(target=anyio.run, args=(_requests,)) for _ in range(2)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Six requests 0.2 s apart take at least 1 s from the first; one limiter per thread would let them through in
    # 0.4 s. A thread waking late only adds to the time, so this cannot fail on a loaded machine.
    assert len(passed) == 6
    assert max(passed) - started >= 1.0
