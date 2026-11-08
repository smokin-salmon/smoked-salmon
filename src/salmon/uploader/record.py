"""What an upload run did, for whoever started it: the torrents it uploaded, and the requests whose outcome it could
not tell and went on past.

The pipeline goes on after a request that may or may not have reached the tracker (an upload whose answer was lost
and not found by its infohash, a lossy master report, a description edit): it says so and moves to the next step,
as the terminal shows. A salmon web job records the run, so it can end with a status of its own for those, and link
the uploaded torrents, and run.main records every command, for what `salmon up` exits with. Outside `recording()`
nothing is kept.
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
    # This is `salmon up`: the only command run.main works a status out for.
    is_up_command: bool = False
    # An upload's POST may have reached a tracker, whatever it answered. `uploads` can't say: it holds no refused
    # upload, and none whose run failed between the POST and its note.
    sent: bool = False
    # The tracker loop ended by itself, with everything it set out to upload up.
    complete: bool = False
    # Each seedbox step that failed (a copy, an add to a client, a client never set up): the seedbox's display name,
    # the step, and the basename of the folder or torrent. Never a secret or a full path.
    seedbox_failures: list[dict[str, str]] = field(default_factory=list)

    @property
    def seedbox_failed(self) -> bool:
        return bool(self.seedbox_failures)

    @property
    def may_be_on_a_tracker(self) -> bool:
        return self.is_up_command and (self.sent or bool(self.unknown_outcomes))

    def exit_status(self, ended: int) -> int:
        """What `salmon up` exits with, given what the command would have (`ended`). `up --help` says what each means.

        Worked out once, at the end of the run, in run.main, so a stop nobody listed is not 0. Ctrl-C, and a bug with
        nothing sent, go through as they always did.
        """
        if self.complete and not self.seedbox_failed and not ended:
            return 0
        return 3 if self.may_be_on_a_tracker else ended or 1


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


def note_up_command() -> None:
    record = _record.get()
    if record is not None:
        record.is_up_command = True


def note_seedbox_failure(seedbox: str, step: str, item: str) -> None:
    record = _record.get()
    if record is not None:
        failure = {"seedbox": seedbox, "step": step, "item": item}
        if failure not in record.seedbox_failures:
            record.seedbox_failures.append(failure)


def note_sent() -> None:
    record = _record.get()
    if record is not None:
        record.sent = True


def note_complete(complete: bool) -> None:
    record = _record.get()
    if record is not None:
        record.complete = complete


def has_unknown_outcome() -> bool:
    record = _record.get()
    return record is not None and bool(record.unknown_outcomes)
