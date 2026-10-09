"""The jobs salmon web runs on an album folder: spectrals, file checks and uploads (ADR 0004, section 5).

Spectrals and checks send nothing anywhere: no tracker request, no image host. Both read the album folder only,
which may be in library_dirs. An upload runs what ``salmon up`` runs (``uploader.run_up``), its questions asked in
the browser: it sends what the command sends for the same album and answers, every tracker request through salmon
web's request loop (``trackers.account``), and works on a copy of a library album, as the command does. Each folder
is checked by ``paths.album_folder`` before the job is queued, and again when it starts, since it may have changed
while the job waited its turn.

Ported from the fork's ``routers/spectrals.py``, ``routers/checks.py`` (chodeus, d6ac6372 and 9bfdddc3) and
``routers/upload.py`` (styx-techno 637ee666, chodeus 0b29d2d5 and 6f846b37), without the spectrals upload and the
tracker checks. The fork's upload called the command's steps itself; the job here runs the command's own code.
"""

import os
from collections.abc import Callable
from typing import Annotated, Any

import anyio.to_thread
import asyncclick as click
import msgspec

import salmon.trackers
from salmon import interaction
from salmon.constants import SOURCES, TAG_ENCODINGS
from salmon.errors import DryRunRefused, UnknownOutcomeError
from salmon.webui import paths
from salmon.webui.jobs import JobError, JobKind, own_folder, register


class SpectralsParams(msgspec.Struct, forbid_unknown_fields=True):
    path: str


class ChecksParams(msgspec.Struct, forbid_unknown_fields=True):
    path: str
    # Also the plain-text report check all --report prints.
    report: bool = False


# Track numbers count from 1: the command's -sp also takes 0 and negative numbers, which pick other tracks.
TrackNumber = Annotated[int, msgspec.Meta(gt=0)]


class UploadParams(msgspec.Struct, forbid_unknown_fields=True):
    """The options of ``salmon up`` a form gives. -yyy and --dry-run are the job's own (``assume_defaults``,
    ``dry_run``)."""

    path: str
    # -s, which the command requires.
    source: str
    # -t, in order. None: the job asks which tracker, as the command does.
    trackers: list[str] = []
    group_id: Annotated[int, msgspec.Meta(gt=0)] | None = None
    skip_flac_upload: bool = False
    request: str | None = None
    source_url: str | None = None
    encoding: str | None = None
    lossy: bool | None = None
    spectrals: list[TrackNumber] = []
    spectrals_after: bool = False
    scene: bool = False
    essential_only: bool = False
    compress: bool = False
    overwrite: bool = False
    auto_rename: bool = False
    skip_up: bool = False
    skip_mqa: bool = False
    skip_log_check: bool = False
    skip_integrity_check: bool = False
    skip_initial_review: bool = False
    apply_ai_suggestions: bool = False


def _checked(params: SpectralsParams | ChecksParams, _dry_run: bool) -> SpectralsParams | ChecksParams:
    return msgspec.structs.replace(params, path=paths.album_folder(params.path))


def _up_options(params: UploadParams, trackers: tuple[str, ...]) -> dict[str, Any]:
    """The arguments of ``uploader.UpOptions`` the form's options make, as the command's callbacks give them."""
    return {
        **msgspec.structs.asdict(params),
        "trackers": trackers,
        "spectrals": tuple(params.spectrals),
        "encoding": TAG_ENCODINGS[params.encoding] if params.encoding else None,
    }


def _checked_upload(params: UploadParams, dry_run: bool) -> UploadParams:
    """The upload's options as the command's callbacks give them, refused where the command refuses them.

    A tracker the config does not have is refused here, where the command would ask for another in the run.
    """
    from salmon.uploader import UpOptions

    source = SOURCES.get(params.source.lower())
    if source is None:
        sources = ", ".join(SOURCES.values())
        raise JobError(400, f"{params.source} is not a valid source. Possible sources are: {sources}")
    # Kept by its name, which the job shows among its parameters.
    encoding = params.encoding.upper() if params.encoding else None
    if encoding is not None and encoding not in TAG_ENCODINGS:
        raise JobError(400, f"{params.encoding} is not a valid encoding.")
    trackers: list[str] = []
    for name in params.trackers:
        code = name.strip().upper()
        if code not in salmon.trackers.tracker_list:
            raise JobError(400, f"{name} is not a tracker in your config.")
        if code not in trackers:
            trackers.append(code)
    checked = msgspec.structs.replace(
        params, path=paths.album_folder(params.path), source=source, encoding=encoding, trackers=trackers
    )
    if (error := UpOptions(**_up_options(checked, tuple(trackers))).usage_error(dry_run)) is not None:
        raise JobError(400, error)
    return checked


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


async def upload(params: UploadParams) -> dict[str, Any]:
    """What ``salmon up`` runs, its questions asked in the browser.

    Returns:
        The folder, the trackers, and each torrent uploaded (none in a dry run).

    Raises:
        UnknownOutcomeError: The run went on past a request that may have reached the tracker (an upload, a lossy
            master report, a description edit), as the command does: the job then ends as unknown_outcome.
        click.ClickException: A dry run stopped before a step that would have sent something.
    """
    from salmon.uploader import UpOptions, run_up
    from salmon.uploader.record import recording

    path = _album(params.path)
    # The command's -t: none given asks which tracker.
    trackers = await salmon.trackers.validate_trackers(None, None, tuple(params.trackers))
    options = UpOptions(**{**_up_options(params, trackers), "path": path})
    with recording() as record:
        try:
            await run_up(options)
        except* DryRunRefused as refused:
            raise click.ClickException(str(refused.exceptions[0])) from None
    if record.unknown_outcomes:
        raise UnknownOutcomeError("; ".join(str(error) for error in record.unknown_outcomes))
    return {"folder": os.path.basename(path), "trackers": list(trackers), "uploads": record.uploads}


def _title(what: str) -> Callable[[Any], str]:
    return lambda params: f"{what}: {os.path.basename(params.path)}"


def _upload_title(params: UploadParams) -> str:
    where = f" to {', '.join(params.trackers)}" if params.trackers else ""
    return f"Upload{where}: {os.path.basename(params.path)}"


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
register(
    JobKind(
        name="upload",
        params=UploadParams,
        run=upload,
        title=_upload_title,
        folder=lambda params: params.path,
        check=_checked_upload,
    )
)
