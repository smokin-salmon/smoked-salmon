"""MusicBrainz's JSON API and the Cover Art Archive, asked as MusicBrainz asks to be (#1065).

One request a second (tests/test_sources_musicbrainz_rate_limit.py), salmon's User-Agent, at most one retry and only
on a failure that may pass, and every failure a ScrapeError naming its type or status, never the URL.
"""

import time
from collections.abc import Iterator

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

    assert _failure() == "MusicBrainz: Request failed (HTTP 503)."
    assert len(server.requests) == 2
    assert sleeps == [2.0]


def test_a_503_then_an_answer_is_the_answer(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.status = [503]

    assert _get_release()["id"] == KIND_OF_BLUE
    assert len(server.requests) == 2


def test_a_short_retry_after_is_waited_for(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.status = [503, 503]
    server.headers = {"Retry-After": "3"}

    _failure()

    assert sleeps == [3.0]


def test_a_long_retry_after_is_not_waited_for(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.status = [503]
    server.headers = {"Retry-After": "120"}

    assert _failure() == "MusicBrainz: Request failed (HTTP 503)."
    assert len(server.requests) == 1
    assert sleeps == []


def test_a_server_that_does_not_answer_times_out(
    server: FakeMusicBrainz, sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MusicBrainzBase, "timeout", 0.2)
    server.delay = 3

    started = time.monotonic()
    assert _failure() == "MusicBrainz: Request failed (TimeoutError)."

    assert time.monotonic() - started < 2
    assert len(server.requests) == 2


def test_a_release_not_on_musicbrainz_is_said_so(server: FakeMusicBrainz) -> None:
    with pytest.raises(musicbrainz.NotFoundError) as excinfo:
        anyio.run(MusicBrainzBase().get_json, "/release/00000000-0000-0000-0000-000000000000")

    assert str(excinfo.value) == "MusicBrainz: Not found."
    assert len(server.requests) == 1


def test_a_500_is_not_retried(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    server.status = [500]

    assert _failure() == "MusicBrainz: Request failed (HTTP 500)."
    assert len(server.requests) == 1


def test_an_answer_that_is_not_json_is_a_scrape_error(server: FakeMusicBrainz) -> None:
    server.body = "<html>Not the release</html>"

    assert _failure() == "MusicBrainz: Did not receive JSON from API."


def test_a_refused_internal_address_is_not_retried(server: FakeMusicBrainz, sleeps: list[float]) -> None:
    with ssrf.public_only():
        assert _failure() == (
            "MusicBrainz: Refused to connect to 127.0.0.1: salmon web only fetches from public addresses."
        )

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

    assert _failure() == f"MusicBrainz: Request failed ({error.__name__})."
    assert len(attempts) == 1
    assert sleeps == []


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
    waited: list[None] = []

    class _Counting(musicbrainz.RateLimiter):
        async def wait(self) -> None:
            waited.append(None)

    monkeypatch.setattr(musicbrainz, "limiter", _Counting(0))
    anyio.run(MusicBrainzBase().fetch_cover_art, KIND_OF_BLUE)
    assert waited == []
    _get_release()
    assert waited == [None]
