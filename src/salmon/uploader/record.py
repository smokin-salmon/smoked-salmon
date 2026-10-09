"""What an upload run did, for whoever started it: the torrents it uploaded, and the requests whose outcome it could
not tell and went on past.

The pipeline goes on after a request that may or may not have reached the tracker (an upload whose answer was lost
and not found by its infohash, a lossy master report, a description edit): it says so and moves to the next step,
as the terminal shows. A salmon web job records the run, so it can end with a status of its own for those, and link
the uploaded torrents. Outside `recording()` nothing is kept.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from salmon.errors import UnknownOutcomeError


@dataclass
class RunRecord:
    # Each torrent uploaded: its tracker, format and URL, in the order they went up.
    uploads: list[dict[str, Any]] = field(default_factory=list)
    # Each request the run went on past without knowing whether the tracker acted on it.
    unknown_outcomes: list[UnknownOutcomeError] = field(default_factory=list)


_record: ContextVar[RunRecord | None] = ContextVar("upload_run_record", default=None)


@contextmanager
def recording() -> Iterator[RunRecord]:
    """Record what the run in the block, and every task it starts, uploads and could not tell."""
    record = RunRecord()
    token = _record.set(record)
    try:
        yield record
    finally:
        _record.reset(token)


def note_upload(tracker: str, format: str, url: str) -> None:
    record = _record.get()
    if record is not None:
        record.uploads.append({"tracker": tracker, "format": format, "url": url})


def note_unknown_outcome(error: UnknownOutcomeError) -> None:
    record = _record.get()
    if record is not None:
        record.unknown_outcomes.append(error)
