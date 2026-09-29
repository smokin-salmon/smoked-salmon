"""`salmon up --dry-run`: go through a whole upload and send nothing.

A dry run makes every check, prompt, torrent and transcode an upload makes, on a scratch copy of the album,
and prints the form each upload would send instead of sending it. Each step that would send something
(a tracker POST, an image host upload, a seedbox copy) skips itself and says so. Behind those skips,
refuse() stops any that was missed: tracker requests other than GET, and image host uploads, call it.

The flag lives in a context variable, set for the length of mode(), so it never outlives the run that set it.
The scratch directory an upload writes into is one too, set for the length of that upload's scratch copy.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Self

import asyncclick as click

from salmon.errors import DryRunRefused

_running: ContextVar[bool] = ContextVar("dry_run", default=False)
# Where an upload in a dry run writes what it would leave behind (torrent files, transcodes).
_scratch_dir: ContextVar[str | None] = ContextVar("dry_run_scratch_dir", default=None)


@contextmanager
def mode(on: bool = True) -> Iterator[None]:
    """Run the block as a dry run (if `on`), and every task it starts."""
    if not on:
        yield
        return
    token = _running.set(True)
    try:
        yield
    finally:
        _running.reset(token)


def active() -> bool:
    """Whether a dry run is running."""
    return _running.get()


def refuse(action: str) -> None:
    """Stop a step that would send something, if a dry run is running.

    Args:
        action: What the step was about to do, e.g. "send POST https://.../upload.php to RED".

    Raises:
        DryRunRefused: In a dry run.
    """
    if active():
        raise DryRunRefused(
            f"Dry run stopped before it could {action}. Nothing was sent. A step that sends something ran "
            "during a dry run: this is a bug in salmon, please report it."
        )


def say(message: str) -> None:
    """Print what a dry run does instead of a step: "Dry run: " and the message."""
    click.secho(f"Dry run: {message}", fg="cyan")


@contextmanager
def writing_into(path: str | None) -> Iterator[None]:
    """Make path the directory an upload in a dry run writes into (see scratch_dir()), for the block only.

    Each upload sets its own, in its own context, so two uploads never write into one another's.

    Args:
        path: The run directory of the upload's scratch copy, removed when the upload ends; None for none.
    """
    token = _scratch_dir.set(path)
    try:
        yield
    finally:
        _scratch_dir.reset(token)


def scratch_dir() -> str:
    """The directory an upload in a dry run writes torrent files and transcodes into, instead of the configured ones.

    It is the run directory of the album's scratch copy, removed when the upload ends, so a dry run leaves no
    torrent file where a torrent client could pick it up.

    Raises:
        RuntimeError: If no dry run is running, or its upload has no scratch copy.
    """
    path = _scratch_dir.get()
    if not active() or path is None:
        raise RuntimeError("no dry run upload with a scratch copy is running")
    return path


class Pending(int):
    """An ID only the tracker gives, for an upload a dry run does not send.

    It stands in for the ID wherever the upload's ID would go (the form of a transcode into the group the
    upload creates, a link to the uploaded torrent) and shows what it stands for there, as a form field, in
    a URL or in a description. It is true and no real ID, so the uploads that follow go into "that" group.
    """

    description: str

    def __new__(cls, description: str) -> Self:
        pending = super().__new__(cls, -1)
        pending.description = description
        return pending

    def __str__(self) -> str:
        return f"<{self.description}>"

    __repr__ = __str__


NEW_GROUP_ID = Pending("ID of the group this upload creates")
NEW_TORRENT_ID = Pending("ID of the uploaded torrent")


def image_url(path: str, host: str) -> str:
    """Stand in for the URL an image host would give the image at path, which a dry run does not upload."""
    return f"<{host} URL of {os.path.basename(path)}>"
