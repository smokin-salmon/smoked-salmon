"""Standalone approval reports exposed as browser jobs."""

from typing import Literal

import msgspec

from salmon import dryrun
from salmon.reports import run_lossy_report
from salmon.webui import paths
from salmon.webui.jobs import JobKind, register


class LossyReportParams(msgspec.Struct, forbid_unknown_fields=True):
    target: str
    tracker: Literal["RED", "OPS"] | None = None
    comment: str | None = None
    source_url: str | None = ""
    folder: str | None = None


def _check(params: LossyReportParams, dry_run: bool) -> LossyReportParams:
    return (
        msgspec.structs.replace(params, folder=paths.album_folder(params.folder))
        if params.folder is not None
        else params
    )


async def lossy_report(params: LossyReportParams) -> dict[str, str]:
    """Use the command's preview and confirmation through browser interaction."""
    text = await run_lossy_report(
        params.target,
        params.tracker,
        params.comment,
        params.source_url,
        dryrun.active(),
        paths.album_folder(params.folder) if params.folder is not None else None,
    )
    return {"text": text}


register(
    JobKind(
        name="lossy_report",
        params=LossyReportParams,
        run=lossy_report,
        title=lambda params: f"Lossy approval report: {params.target}",
        check=_check,
        folder=lambda params: params.folder,
        exclusive=True,
    )
)
