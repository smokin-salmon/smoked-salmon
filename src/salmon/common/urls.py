import math
from datetime import UTC
from email.utils import parsedate_to_datetime
from time import time


def parse_retry_after(value: str | None) -> float | None:
    """Get the wait in seconds from a Retry-After header (delay-seconds or HTTP-date).

    A non-finite or negative delay (``nan``, ``inf``, ``-5``), an unparseable value and an
    HTTP-date already in the past all count as no wait given, so the caller's own fallback
    applies instead of hanging forever or retrying immediately in a loop.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        # A negative delay is invalid (RFC 9110).
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        # An HTTP-date with a "-0000" offset parses as naive; that means UTC, not local time.
        when = when.replace(tzinfo=UTC)
    wait = when.timestamp() - time()
    return wait if wait > 0 else None
