"""Shared policy for mutations that touch many files in one album.

None of these can be made atomic: writing tags to twelve files is twelve
writes, so when one fails partway the album is left inconsistent. Continuing
would upload a release whose tracks disagree with each other, so every such
failure reports how far it got and stops.
"""

from typing import NoReturn

import asyncclick as click

from salmon.errors import UploadError


def abort_partial(
    operation: str,
    done: list[str],
    failed: str | None,
    remaining: list[str],
    error: BaseException,
) -> NoReturn:
    """Report how far a multi-file mutation got, then stop the run with an UploadError.

    `failed` is the file being written when it broke. A write can fail after
    changing the file, so its state is unknown rather than untouched: reporting
    it as unchanged would send someone looking in the wrong place.
    """
    if done:
        click.secho(f"Already changed: {', '.join(done)}.", fg="yellow")
    if failed:
        click.secho(f"May be partially changed: {failed}.", fg="red", bold=True)
    if remaining:
        click.secho(f"Not changed: {', '.join(remaining)}.", fg="yellow")
    # An error, not click.Abort: an abort ends an upload quietly, as if the user had said no.
    state = ": the album is now inconsistent, repair it before uploading" if done or failed else ""
    raise UploadError(f"{operation} failed ({error}){state}.") from error
