import os
import sys
import time
from collections.abc import Iterator
from email.utils import formatdate
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.common.urls import parse_retry_after


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


@pytest.mark.parametrize(("value", "seconds"), [("120", 120.0), ("0", 0.0), ("2.5", 2.5)])
def test_delay_seconds(value: str, seconds: float) -> None:
    assert parse_retry_after(value) == seconds


@pytest.mark.parametrize("value", [None, "", "soon", "nan", "inf", "-inf", "1e400", "-5"])
def test_no_valid_wait_is_none(value: str | None) -> None:
    assert parse_retry_after(value) is None


@pytest.mark.parametrize("usegmt", [True, False], ids=["gmt", "naive"])
@pytest.mark.usefixtures("far_from_utc")
def test_http_date_is_the_wait_until_then_in_utc(usegmt: bool) -> None:
    wait = parse_retry_after(formatdate(time.time() + 30, usegmt=usegmt))
    assert wait is not None
    assert 28 < wait <= 30


def test_http_date_already_past_is_none() -> None:
    assert parse_retry_after(formatdate(time.time() - 30, usegmt=True)) is None
