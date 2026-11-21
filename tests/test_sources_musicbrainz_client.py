"""MusicBrainz's JSON API and the Cover Art Archive, asked as MusicBrainz asks to be (#1065).

One request a second (tests/test_sources_musicbrainz_rate_limit.py), salmon's User-Agent, at most one retry and only
on a failure that may pass, and every failure a ScrapeError naming its type or status, never the URL.
"""

import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import anyio
import pytest
from helpers.musicbrainz import FakeMusicBrainz, serving_musicbrainz

from salmon import ssrf
from salmon.errors import ScrapeError
from salmon.release_notification import get_version
from salmon.sources import musicbrainz
from salmon.sources.musicbrainz import MusicBrainzBase

KIND_OF_BLUE = "4c8af71f-5746-4981-92d7-0d6c64db1155"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMusicBrainz]:
    with serving_musicbrainz(monkeypatch) as fake:
        yield fake


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every wait between attempts, recorded instead of slept."""
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(musicbrainz.anyio, "sleep", _sleep)
    return slept


def _get_release() -> dict:
    return anyio.run(MusicBrainzBase().get_json, f"/release/{KIND_OF_BLUE}", {"inc": "media"})


def _failure(call=_get_release) -> str:
    with pytest.raises(ScrapeError) as excinfo:
        call()
    return str(excinfo.value)


def test_a_release_is_asked_for_as_json(server: FakeMusicBrainz) -> None:
    release = _get_release()

    assert release["id"] == KIND_OF_BLUE
    [(path, query, _headers)] = server.requests
    assert path == f"/ws/2/release/{KIND_OF_BLUE}"
    assert query == {"inc": "media", "fmt": "json"}


def test_requests_carry_salmons_user_agent(server: FakeMusicBrainz) -> None:
    _get_release()
    anyio.run(MusicBrainzBase().fetch_cover_art, KIND_OF_BLUE)

    for _path, _query, headers in server.requests:
        assert headers["User-Agent"] == (
            f"smoked-salmon/{get_version()} ( https://github.com/smokin-salmon/smoked-salmon/ )"
        )
        assert headers["Accept"] == "application/json"


def test_a_503_is_retried_once_after_two_seconds(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.status = [503, 503]

    message = _failure()

    assert message == "MusicBrainz: Request failed (HTTP 503)."
    assert len(server.requests) == 2
    assert sleeps == [2.0]


def test_a_503_then_an_answer_is_the_answer(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.status = [503]

    release = _get_release()

    assert release["id"] == KIND_OF_BLUE
    assert len(server.requests) == 2


def test_a_short_retry_after_is_waited_for(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.status = [503, 503]
    server.headers = {"Retry-After": "3"}

    _failure()

    assert sleeps == [3.0]


def test_a_long_retry_after_is_not_waited_for(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.status = [503]
    server.headers = {"Retry-After": "120"}

    message = _failure()

    assert message == "MusicBrainz: Request failed (HTTP 503)."
    assert len(server.requests) == 1
    assert sleeps == []


def test_a_server_that_does_not_answer_times_out(
    server: FakeMusicBrainz, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MusicBrainzBase, "timeout", 0.2)
    server.delay = 3

    started = time.monotonic()
    message = _failure()

    assert message == "MusicBrainz: Request failed (TimeoutError)."

    assert time.monotonic() - started < 2
    assert len(server.requests) == 2


def test_a_release_not_on_musicbrainz_is_said_so(server: FakeMusicBrainz) -> None:
    with pytest.raises(musicbrainz.NotFoundError) as excinfo:
        anyio.run(MusicBrainzBase().get_json, "/release/00000000-0000-0000-0000-000000000000")

    assert str(excinfo.value) == "MusicBrainz: Not found."
    assert len(server.requests) == 1


def test_a_500_is_not_retried(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.status = [500]

    message = _failure()

    assert message == "MusicBrainz: Request failed (HTTP 500)."
    assert len(server.requests) == 1


def test_an_answer_that_is_not_json_is_a_scrape_error(server: FakeMusicBrainz) -> None:
    server.body = "<html>Not the release</html>"

    message = _failure()

    assert message == "MusicBrainz: Did not receive JSON from API."


def test_a_refused_internal_address_is_not_retried(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    with ssrf.public_only():
        message = _failure()

    assert message == ("MusicBrainz: Refused to connect to 127.0.0.1: salmon web only fetches from public addresses.")

    assert server.requests == []
    assert sleeps == []


@pytest.mark.parametrize("error", [aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError])
def test_a_tls_failure_is_not_retried(
    error: type[Exception], sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A certificate the client refuses (an intercepting proxy, say) is refused again: as the tracker client, no retry.
    attempts: list[None] = []

    async def _refused(*_args: object, **_kwargs: object) -> None:
        attempts.append(None)
        raise error.__new__(error)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", _refused)

    message = _failure()

    assert message == f"MusicBrainz: Request failed ({error.__name__})."
    assert len(attempts) == 1
    assert sleeps == []


MERGED = "11111111-1111-1111-1111-111111111111"


def _counted(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Every turn taken on MusicBrainz's limiter, counted, with no wait."""
    turns: list[None] = []

    class _Counting(musicbrainz.RateLimiter):
        @asynccontextmanager
        async def turn(self) -> AsyncGenerator[None]:
            turns.append(None)
            yield

    monkeypatch.setattr(musicbrainz, "limiter", _Counting(0))
    return turns


def _merge(server: FakeMusicBrainz) -> None:
    # MusicBrainz answers a merged release's old MBID with a 301 to the release it was merged into.
    server.redirects = {f"/ws/2/release/{MERGED}": f"/ws/2/release/{KIND_OF_BLUE}?inc=media&fmt=json"}


def test_a_merged_release_is_followed_each_hop_on_a_turn_of_its_own(
    server: FakeMusicBrainz, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns = _counted(monkeypatch)
    _merge(server)

    release = anyio.run(MusicBrainzBase().get_json, f"/release/{MERGED}", {"inc": "media"})

    assert release["id"] == KIND_OF_BLUE
    assert [path for path, _query, _headers in server.requests] == [
        f"/ws/2/release/{MERGED}",
        f"/ws/2/release/{KIND_OF_BLUE}",
    ]
    assert len(turns) == 2


def test_a_redirects_hops_are_a_second_apart(server: FakeMusicBrainz, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(musicbrainz, "limiter", musicbrainz.RateLimiter(0.3))
    _merge(server)

    anyio.run(MusicBrainzBase().get_json, f"/release/{MERGED}", {"inc": "media"})

    first, second = server.times
    assert second - first >= 0.25


def test_a_slow_connection_does_not_send_its_request_beside_the_next(
    server: FakeMusicBrainz, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A turn is taken before connecting: had the next request's turn come while the first was still connecting
    # (through a proxy, say), the two would have reached MusicBrainz together.
    monkeypatch.setattr(musicbrainz, "limiter", musicbrainz.RateLimiter(0.3))
    connect = aiohttp.TCPConnector._create_connection
    slowed: list[None] = []

    async def _slow_first(self: aiohttp.TCPConnector, *args: Any, **kwargs: Any) -> Any:
        if not slowed:
            slowed.append(None)
            await anyio.sleep(0.5)
        return await connect(self, *args, **kwargs)

    monkeypatch.setattr(aiohttp.TCPConnector, "_create_connection", _slow_first)

    async def _two() -> None:
        async with anyio.create_task_group() as tasks:
            for _ in range(2):
                tasks.start_soon(MusicBrainzBase().get_json, f"/release/{KIND_OF_BLUE}")

    anyio.run(_two)

    first, second = server.times
    assert second - first >= 0.25


def test_redirects_are_bounded(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.redirects = {f"/ws/2/release/{MERGED}": f"/ws/2/release/{MERGED}"}

    message = _failure(lambda: anyio.run(MusicBrainzBase().get_json, f"/release/{MERGED}"))

    assert message == "MusicBrainz: Request failed (too many redirects)."
    assert len(server.requests) == musicbrainz.MAX_REDIRECTS + 1
    assert sleeps == []


def test_a_dropped_connection_is_sent_twice_in_all(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    # aiohttp would send it again itself, and the retry again: four requests. The retry is the only one.
    server.drops = 10

    message = _failure()

    assert message == "MusicBrainz: Request failed (ServerDisconnectedError)."
    assert len(server.requests) == 2
    assert sleeps == [2.0]


def test_a_dropped_connection_then_an_answer_is_the_answer(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.drops = 1

    release = _get_release()

    assert release["id"] == KIND_OF_BLUE
    assert len(server.requests) == 2


def test_the_cover_art_archives_redirect_is_followed_without_the_limiter(
    server: FakeMusicBrainz, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Cover Art Archive sends its image list on to archive.org.
    turns = _counted(monkeypatch)
    server.redirects = {f"/release/{MERGED}": f"/release/{KIND_OF_BLUE}"}

    images = anyio.run(MusicBrainzBase().fetch_cover_art, MERGED)

    assert images["images"][0]["front"] is True
    assert len(server.requests) == 2
    assert turns == []


def test_the_cover_art_archive_is_asked_for_its_image_list(server: FakeMusicBrainz) -> None:
    images = anyio.run(MusicBrainzBase().fetch_cover_art, KIND_OF_BLUE)

    assert images["images"][0]["front"] is True
    assert [path for path, _query, _headers in server.requests] == [f"/release/{KIND_OF_BLUE}"]


def test_a_release_with_no_cover_art_is_not_found(server: FakeMusicBrainz) -> None:
    with pytest.raises(musicbrainz.NotFoundError):
        anyio.run(MusicBrainzBase().fetch_cover_art, "00000000-0000-0000-0000-000000000000")


def test_the_cover_art_archive_does_not_wait_on_musicbrainzs_limiter(
    server: FakeMusicBrainz, monkeypatch: pytest.MonkeyPatch
) -> None:
    waited = _counted(monkeypatch)
    anyio.run(MusicBrainzBase().fetch_cover_art, KIND_OF_BLUE)
    assert waited == []
    _get_release()
    assert waited == [None]
