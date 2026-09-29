"""How long a tracker request waits when the tracker rate limits it (a 429, with or without Retry-After).

Each test runs a local fake tracker that always answers 429, counts the requests it gets, and records
the waits instead of sleeping them: a wait is never shorter than the rate limiter's spacing, so the
retries cannot burst, and the waits of one request add up to 60 s at most, so
a tracker asking for a long wait stops the request with a clear error instead of hanging the run.
"""

import asyncio
import os
import sys
import time
from collections.abc import Callable, Iterator
from email.utils import formatdate
from pathlib import Path

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from tenacity import wait_none

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.errors import RateLimitedError, RequestError, RequestFailedError
from salmon.trackers.base import BaseGazelleApi, RetryableError


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.api_key = "an-api-key"
        super().__init__()
        # Per instance, as each test runs on its own event loop.
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without the backoff between attempts, so only the rate limit waits are left."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits the requests make, recorded instead of slept."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(seconds: float, *args, **kwargs) -> None:
        # tenacity's backoff, set to none above, still sleeps 0 s between attempts.
        if seconds != 0:
            recorded.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return recorded


@pytest.fixture
def far_from_utc() -> Iterator[None]:
    """Local time 9 hours ahead of UTC, so a date without a zone read as local time is 9 hours off."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "JST-9"
    time.tzset()
    yield
    if before is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = before
    time.tzset()


def _in(seconds: float, *, usegmt: bool = True) -> str:
    """An HTTP-date `seconds` from now; with usegmt=False, the "-0000" form that parses without a zone."""
    return formatdate(time.time() + seconds, usegmt=usegmt)


async def _rate_limited(
    retry_after: Callable[[], str | None], method: str = "GET"
) -> tuple[list[str], BaseException | None]:
    """Send one request to a tracker that rate limits every request.

    Returns:
        The requests the tracker got, and what the request raised.
    """
    hits = []

    async def ajax(request: web.Request) -> web.Response:
        await request.read()
        hits.append(request.method)
        value = retry_after()
        headers = {} if value is None else {"Retry-After": value}
        return web.json_response({"status": "failure", "error": "Rate limit exceeded"}, status=429, headers=headers)

    app = web.Application()
    app.router.add_route("*", "/ajax.php", ajax)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    api = FakeApi(url)
    try:
        # Real time: a wait that is slept, not recorded, fails here instead of hanging the run.
        with anyio.fail_after(5):
            if method == "POST":
                await api._request("POST", url + "/ajax.php?action=upload", data={"file": "x"}, prefer_api_key=True)
            else:
                await api._request("GET", url + "/ajax.php", params={"action": "browse"}, prefer_api_key=True)
    except RequestError as err:
        return hits, err
    finally:
        await api.close()
        await runner.cleanup()
    return hits, None


def test_retry_after_in_seconds_is_waited_for(waits: list[float]) -> None:
    hits, err = anyio.run(_rate_limited, lambda: "5")
    # Each answer is waited for, the last one too: the attempt does not know it is the last.
    assert waits == [5] * 5
    # The tracker still rate limits the last attempt: the retries ran out.
    assert len(hits) == 5
    assert isinstance(err, RetryableError)


@pytest.mark.parametrize(
    "retry_after",
    [
        pytest.param(lambda: None, id="none"),
        pytest.param(lambda: "", id="empty"),
        pytest.param(lambda: "soon", id="unparseable"),
        pytest.param(lambda: "nan", id="nan"),
        pytest.param(lambda: "inf", id="inf"),
        pytest.param(lambda: "-inf", id="-inf"),
        pytest.param(lambda: "-5", id="negative"),
        pytest.param(lambda: _in(-60), id="past-date"),
    ],
)
def test_missing_or_invalid_retry_after_waits_the_default(
    waits: list[float], retry_after: Callable[[], str | None]
) -> None:
    hits, err = anyio.run(_rate_limited, retry_after)
    # 20 s each time, until a fourth wait would take the request past 60 s in all.
    assert waits == [20, 20, 20]
    assert len(hits) == 4
    assert isinstance(err, RateLimitedError)


@pytest.mark.parametrize("retry_after", ["0", "0.2", "1"])
def test_short_retry_after_does_not_retry_at_once(waits: list[float], retry_after: str) -> None:
    hits, err = anyio.run(_rate_limited, lambda: retry_after)
    # At least the rate limiter's own spacing (5 requests per 10 s) between attempts.
    assert waits == [2] * 5
    assert len(hits) == 5
    assert isinstance(err, RetryableError)


@pytest.mark.parametrize(
    "retry_after",
    [
        pytest.param(lambda: "61", id="61"),
        pytest.param(lambda: "3600", id="3600"),
        pytest.param(lambda: "1e9", id="1e9"),
        pytest.param(lambda: _in(3600), id="date-in-an-hour"),
    ],
)
def test_long_retry_after_stops_without_waiting(
    waits: list[float], capsys: pytest.CaptureFixture[str], retry_after: Callable[[], str | None]
) -> None:
    hits, err = anyio.run(_rate_limited, retry_after)
    assert waits == []
    assert len(hits) == 1
    assert isinstance(err, RateLimitedError)
    # Not an answer about what was asked for: a caller must not read it as "no such torrent".
    assert not isinstance(err, RequestFailedError)
    assert str(err).startswith("RED asks to wait ")
    assert "try again later" in capsys.readouterr().out


def test_waits_of_one_request_add_up_to_60_seconds_at_most(
    waits: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    hits, err = anyio.run(_rate_limited, lambda: "30")
    assert waits == [30, 30]
    assert len(hits) == 3
    assert isinstance(err, RateLimitedError)
    assert "RED asks to wait 30 s more (after 60 s already)" in capsys.readouterr().out


@pytest.mark.parametrize("usegmt", [True, False], ids=["gmt", "naive"])
@pytest.mark.usefixtures("far_from_utc")
def test_retry_after_as_a_date_is_waited_for(waits: list[float], usegmt: bool) -> None:
    hits, err = anyio.run(_rate_limited, lambda: _in(30, usegmt=usegmt))
    # Whole seconds, rounded up: the date has no fraction of a second.
    assert waits == [30, 30]
    assert len(hits) == 3
    assert isinstance(err, RateLimitedError)


def test_rate_limited_upload_asking_for_a_long_wait_is_not_sent_again(waits: list[float]) -> None:
    hits, err = anyio.run(_rate_limited, lambda: "3600", "POST")
    assert waits == []
    assert hits == ["POST"]
    # A 429 means the tracker did not take the upload: its outcome is known.
    assert isinstance(err, RateLimitedError)


def test_rate_limited_upload_waits_before_it_is_sent_again(waits: list[float]) -> None:
    hits, err = anyio.run(_rate_limited, lambda: "0", "POST")
    assert waits == [2] * 5
    assert hits == ["POST"] * 5
    assert isinstance(err, RetryableError)
