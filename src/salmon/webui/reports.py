"""Standalone approval reports exposed as browser jobs."""

from typing import Literal

import msgspec

from salmon import dryrun
from salmon.reports import run_lossy_report
from salmon.webui.jobs import JobKind, register


class LossyReportParams(msgspec.Struct, forbid_unknown_fields=True):
    target: str
    tracker: Literal["RED", "OPS"] | None = None
    report_type: Literal["master", "web"] | None = None
    comment: str | None = None
    source_url: str | None = None


async def lossy_report(params: LossyReportParams) -> dict[str, str]:
    """Use the command's preview and confirmation through browser interaction."""
    text = await run_lossy_report(
        params.target, params.report_type, params.tracker, params.comment, params.source_url, dryrun.active()
    )
    return {"text": text}


register(
    JobKind(
        name="lossy_report",
        params=LossyReportParams,
        run=lossy_report,
        title=lambda params: f"Lossy approval report: {params.target}",
        exclusive=True,
    )
)
