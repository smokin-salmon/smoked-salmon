"""The session cookie and API key a tracker client is built with (#1034, ADR 0008).

salmon web reads them from config.toml when a job starts and sets them here for that job, so a change saved on its
Settings page reaches every job started after it, and a running job keeps what it started with. Without a snapshot
(the CLI, anything outside a job), they are the config's as loaded. ``cfg`` is never changed.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

import asyncclick as click

import salmon.config
import salmon.trackers
from salmon import cfg
from salmon.config.edit import TRACKER_CODES, Credentials
from salmon.trackers.base import session_cookie_forms

_snapshot: ContextVar[Mapping[str, Credentials] | None] = ContextVar("tracker_credentials", default=None)


class TrackerRemovedError(click.ClickException):
    """A tracker salmon web started with is no longer in the config the job read."""


def from_cfg() -> dict[str, Credentials]:
    """Each configured tracker's credentials as the config gave them when salmon started, by site code."""
    found: dict[str, Credentials] = {}
    for code in TRACKER_CODES:
        tracker = getattr(cfg.tracker, code.lower())
        if tracker is not None:
            found[code] = Credentials(tracker.session, tracker.api_key)
    return found


@contextmanager
def using(snapshot: Mapping[str, Credentials]) -> Iterator[None]:
    """Build every tracker client in the block, and in the tasks it starts, with these credentials."""
    token = _snapshot.set(snapshot)
    try:
        yield
    finally:
        _snapshot.reset(token)


def credentials(code: str) -> Credentials | None:
    """Tracker ``code``'s credentials: the job's snapshot when one is set, else the config's. None if not configured.

    Raises:
        TrackerRemovedError: If salmon started with the tracker, and the config the job read no longer has it.
    """
    snapshot = _snapshot.get()
    if snapshot is None:
        return from_cfg().get(code)
    if code not in snapshot and code in salmon.trackers.tracker_list:
        raise TrackerRemovedError(
            f"{code} is no longer in {salmon.config.loaded_config_path or 'the config'}: adding or removing a tracker "
            "takes effect after salmon web restarts."
        )
    return snapshot.get(code)


def secrets_of(snapshot: Mapping[str, Credentials]) -> list[str]:
    """Every form of each cookie in the snapshot (as configured, decoded, as sent) and each API key."""
    found: list[str] = []
    for creds in snapshot.values():
        if creds.session.strip():
            found += session_cookie_forms(creds.session)
        if creds.api_key:
            found.append(creds.api_key)
    return list(dict.fromkeys(secret for secret in found if secret))
