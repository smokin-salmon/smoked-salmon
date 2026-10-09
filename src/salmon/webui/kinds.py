"""The jobs salmon web runs on an album folder: spectrals and file checks (ADR 0004, section 5).

Neither sends anything anywhere: no tracker request, no image host. Both read the album folder only, which may be
in library_dirs. Each folder is checked by ``paths.album_folder`` before the job is queued, and again when it
starts, since it may have changed while the job waited its turn.

Ported from the fork's ``routers/spectrals.py`` and ``routers/checks.py`` (chodeus, d6ac6372 and 9bfdddc3), without
the spectrals upload and the tracker checks.
"""

import os
from collections.abc import Callable
from typing import Any

import anyio.to_thread
import asyncclick as click
import msgspec

from salmon import interaction
from salmon.webui import paths
from salmon.webui.jobs import JobKind, own_folder, register


class SpectralsParams(msgspec.Struct, forbid_unknown_fields=True):
    path: str


class ChecksParams(msgspec.Struct, forbid_unknown_fields=True):
    path: str
    # Also the plain-text report check all --report prints.
    report: bool = False


def _checked(params: SpectralsParams | ChecksParams) -> SpectralsParams | ChecksParams:
    return msgspec.structs.replace(params, path=paths.album_folder(params.path))


def _album(path: str) -> str:
    """The album folder, checked again as the job starts."""
    try:
        return paths.album_folder(path)
    except paths.PathRefused as e:
        raise click.ClickException(e.detail) from None


async def spectrals(params: SpectralsParams) -> dict[str, Any]:
    """What ``salmon specs`` shows: the spectrals, the frequency analysis and its plots, never uploaded.

    Everything is written into a folder of the job's own (see ``own_folder``), never next to the music.
    """
    # sox, numpy, PyAV and Pillow: loaded when the job runs, not when the server starts.
    from salmon.tagger.audio_info import gather_audio_info
    from salmon.uploader.spectrals import generate_spectrals_all, print_frequency_analysis

    path = _album(params.path)
    audio_info = await anyio.to_thread.run_sync(gather_audio_info, path, True)
    folder = own_folder("spectrals")
    spectral_ids = await generate_spectrals_all(path, folder, audio_info)
    marks_found = await print_frequency_analysis(path, folder, spectral_ids)
    await interaction.show_spectrals(folder, spectral_ids)
    return {
        "folder": os.path.basename(path),
        "tracks": {f"{spectral_id:02d}": name for spectral_id, name in spectral_ids.items()},
        "marks_found": marks_found,
    }


async def checks(params: ChecksParams) -> dict[str, Any]:
    """``salmon check all`` without a tracker: the verdict rows, and the report if asked. Changes nothing."""
    from salmon.checks import print_album_checks
    from salmon.checks.album import check_album
    from salmon.checks.report import build_report

    path = _album(params.path)
    found = await check_album(path, [])
    print_album_checks(found)
    return {
        "folder": found.folder,
        "rows": [
            {"verdict": row.verdict, "check": row.check, "detail": row.detail, "notes": list(row.notes)}
            for row in found.rows
        ],
        "blocking": len(found.blocking),
        "warnings": sum(row.verdict == "WARN" for row in found.rows),
        "report": build_report(found) if params.report else None,
    }


def _title(what: str) -> Callable[[Any], str]:
    return lambda params: f"{what}: {os.path.basename(params.path)}"


register(
    JobKind(
        name="spectrals",
        params=SpectralsParams,
        run=spectrals,
        title=_title("Spectrals"),
        folder=lambda params: params.path,
        check=_checked,
    )
)
register(
    JobKind(
        name="checks",
        params=ChecksParams,
        run=checks,
        title=_title("Checks"),
        folder=lambda params: params.path,
        check=_checked,
    )
)
