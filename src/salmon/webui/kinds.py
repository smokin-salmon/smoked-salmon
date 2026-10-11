"""The jobs salmon web runs: on an album folder (spectrals, file checks, uploads, conversions) and the
connection check (ADR 0004, section 5).

Spectrals send nothing anywhere: no tracker request, no image host. Checks send nothing either unless trackers are
named: then each named tracker gets what ``salmon check all -t`` sends it, salmon up's dupe search (the index call,
then one browse per search string), through salmon web's request loop. Both read the album folder only, which may
be in library_dirs. An upload runs what ``salmon up`` runs (``uploader.run_up``), its questions asked in the browser:
it sends what the command sends for the same album and answers, every tracker request through salmon web's request
loop (``trackers.account``), and works on a copy of a library album, as the command does. A conversion (transcode,
downconvert) or a recompression (compress) runs what ``salmon transcode``, ``salmon downconv`` and ``salmon compress``
run and sends nothing anywhere: a conversion writes where the command puts it (beside the album, or for a library
album under download_directory), a recompression writes in place and is refused for a library album. A tag job runs
what ``salmon tag`` runs (``tagger.run_tag``), its questions asked in the browser: it sends nothing to a tracker, and
tags a library album as a copy renamed into download_directory, never in place. Each folder is
checked by ``paths.album_folder`` before the job is queued, and again when it starts, since it may have changed while
the job waited its turn.

The connection check is ``salmon checkconf -t`` for the trackers asked for: at most two requests each (the index
call with the session cookie, then with the API key when one is set), through salmon web's request loop, and only
when the user starts it, never on page load. Metadata sources and seedboxes stay with the command.

Ported from the fork's ``routers/spectrals.py``, ``routers/checks.py`` (chodeus, d6ac6372, 9bfdddc3 and 889cc4f5),
``routers/upload.py`` (styx-techno 637ee666, chodeus 0b29d2d5 and 6f846b37), ``routers/convert.py`` (chodeus
0b29d2d5, a70a3d75, d8c68c28 and a18e0e25), without the spectrals upload, and ``checks/connection.py`` (chodeus,
0b29d2d5, 9bfdddc3 and 75e5a3d9), without its run on load and its cache. The fork's upload and conversions called
the commands' steps themselves; the jobs here run the commands' own code.
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

import anyio.to_thread
import asyncclick as click
import msgspec

import salmon.trackers
from salmon import interaction
from salmon.constants import SOURCES, TAG_ENCODINGS
from salmon.converter.transcoding import Bitrate
from salmon.errors import DryRunRefused, UnknownOutcomeError
from salmon.trackers.connection import (
    check_connection,
    print_certificate_failure,
    print_session_cookie_header,
    print_step,
    print_tracker_header,
    print_verdict,
)
from salmon.webui import paths
from salmon.webui.jobs import JobError, JobKind, own_folder, register

if TYPE_CHECKING:
    from salmon.uploader import UpOptions


class SpectralsParams(msgspec.Struct, forbid_unknown_fields=True):
    path: str


class ChecksParams(msgspec.Struct, forbid_unknown_fields=True):
    path: str
    # Also the plain-text report check all --report prints.
    report: bool = False
    # check all's -t: the trackers to search for a dupe, whose rules apply. None contacts no tracker.
    trackers: list[str] = []


class TranscodeParams(msgspec.Struct, forbid_unknown_fields=True):
    """``salmon transcode``: -b and -eo."""

    path: str
    bitrate: Bitrate
    essential_only: bool = False


class DownconvertParams(msgspec.Struct, forbid_unknown_fields=True):
    """``salmon downconv``: -eo."""

    path: str
    essential_only: bool = False


class CompressParams(msgspec.Struct, forbid_unknown_fields=True):
    path: str


class ConnectionCheckParams(msgspec.Struct, forbid_unknown_fields=True):
    # checkconf's -t: the trackers to check, in order. None: every tracker in the config.
    trackers: list[str] = []


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


class TagParams(msgspec.Struct, forbid_unknown_fields=True):
    """The options of ``salmon tag`` a form gives. -yyy is the job's own (``assume_defaults``); the command has no
    dry run."""

    path: str
    # -s, which the command requires.
    source: str
    # -e, needed when the files are not lossless.
    encoding: str | None = None
    overwrite: bool = False
    auto_rename: bool = False
    skip_initial_review: bool = False
    apply_ai_suggestions: bool = False


def _checked(params: SpectralsParams | ChecksParams, _dry_run: bool) -> SpectralsParams | ChecksParams:
    return msgspec.structs.replace(params, path=paths.album_folder(params.path))


def _checked_checks(params: ChecksParams, _dry_run: bool) -> ChecksParams:
    """The folder checked, and the trackers as check all's -t takes them: a tracker not in the config is refused
    before the job starts, as the command refuses it before it runs."""
    try:
        trackers = salmon.trackers.tracker_codes(params.trackers)
    except salmon.trackers.UnknownTrackerError as e:
        raise JobError(422, str(e)) from None
    return msgspec.structs.replace(params, path=paths.album_folder(params.path), trackers=trackers)


def _checked_connection_check(params: ConnectionCheckParams, _dry_run: bool) -> ConnectionCheckParams:
    """The trackers to check: those named, each in the config, else all of those in the config."""
    try:
        trackers = salmon.trackers.tracker_codes(params.trackers) or list(salmon.trackers.tracker_list)
    except salmon.trackers.UnknownTrackerError as e:
        raise JobError(422, str(e)) from None
    if not trackers:
        raise JobError(422, "No tracker is configured.")
    return msgspec.structs.replace(params, trackers=trackers)


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


def _checked_tag(params: TagParams, dry_run: bool) -> TagParams:
    """The tag options as the command's callbacks give them, refused where the command refuses them."""
    # The command has none, and tagging writes the files and renames the folder: nothing a dry run could leave out.
    if dry_run:
        raise JobError(400, "Tag has no dry run: it retags and renames the album.")
    source = SOURCES.get(params.source.lower())
    if source is None:
        sources = ", ".join(SOURCES.values())
        raise JobError(400, f"{params.source} is not a valid source. Possible sources are: {sources}")
    # Kept by its name, which the job shows among its parameters.
    encoding = params.encoding.upper() if params.encoding else None
    if encoding is not None and encoding not in TAG_ENCODINGS:
        raise JobError(400, f"{params.encoding} is not a valid encoding.")
    return msgspec.structs.replace(params, path=paths.album_folder(params.path), source=source, encoding=encoding)


def _checked_conversion(
    params: TranscodeParams | DownconvertParams, dry_run: bool
) -> TranscodeParams | DownconvertParams:
    # The commands have no dry run, and a conversion's record is the one thing a dry run would leave out.
    if dry_run:
        raise JobError(400, "A conversion has no dry run: it writes a new folder and leaves the album as it is.")
    return msgspec.structs.replace(params, path=paths.album_folder(params.path))


def _checked_compress(params: CompressParams, dry_run: bool) -> CompressParams:
    """The folder, refused where ``salmon compress`` refuses it: it recompresses in place."""
    from salmon.commands import compress_refusal

    if dry_run:
        raise JobError(400, "Compress has no dry run: it recompresses the album in place.")
    path = paths.album_folder(params.path)
    if (refusal := compress_refusal(path)) is not None:
        raise JobError(403, refusal)
    return msgspec.structs.replace(params, path=path)


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
    """``salmon check all``, with -t for each tracker named: the verdict rows, and the report if asked.

    Changes nothing. Each named tracker gets the command's dupe search, through salmon web's request loop.
    """
    from salmon.checks import print_album_checks
    from salmon.checks.album import check_album
    from salmon.checks.report import build_report

    path = _album(params.path)
    found = await check_album(path, params.trackers)
    print_album_checks(found)
    return {
        "folder": found.folder,
        "trackers": params.trackers,
        "rows": [
            {"verdict": row.verdict, "check": row.check, "detail": row.detail, "notes": list(row.notes)}
            for row in found.rows
        ],
        "blocking": len(found.blocking),
        "warnings": sum(row.verdict == "WARN" for row in found.rows),
        "report": build_report(found) if params.report else None,
    }


async def transcode(params: TranscodeParams) -> dict[str, Any]:
    """What ``salmon transcode`` runs: the album transcoded to MP3, where the command puts it."""
    from salmon.converter import run_transcode

    path = _album(params.path)
    return {"output": await run_transcode(path, params.bitrate, params.essential_only)}


async def downconvert(params: DownconvertParams) -> dict[str, Any]:
    """What ``salmon downconv`` runs: the album downconverted to 16 bit, where the command puts it."""
    from salmon.converter import run_downconv

    path = _album(params.path)
    return {"output": await run_downconv(path, params.essential_only)}


async def compress(params: CompressParams) -> dict[str, Any]:
    """What ``salmon compress`` runs: the album's FLACs recompressed in place, never a library album's."""
    from salmon.commands import compress_refusal, recompress_flacs

    path = _album(params.path)
    # Again: the config or the folder may have changed while the job waited.
    if (refusal := compress_refusal(path)) is not None:
        raise click.ClickException(refusal)
    return {"folder": path, "recompressed": await recompress_flacs(path)}


async def upload(params: UploadParams) -> dict[str, Any]:
    """What ``salmon up`` runs, its questions asked in the browser.

    Returns:
        The folder, the trackers, and each torrent uploaded (none in a dry run).

    Raises:
        UnknownOutcomeError: The run went on past a request that may have reached the tracker (an upload, a lossy
            master report, a description edit), as the command does: the job then ends as unknown_outcome.
        click.ClickException: A dry run stopped before a step that would have sent something.
    """
    from salmon.uploader import UpOptions
    from salmon.uploader.record import recording

    path = _album(params.path)
    # The command's -t: none given asks which tracker.
    trackers = await salmon.trackers.validate_trackers(None, None, tuple(params.trackers))
    options = UpOptions(**{**_up_options(params, trackers), "path": path})
    with recording() as record:
        try:
            await _run_up(options)
        except BaseException as err:
            # Also when the run stops later (an abort, a cancel): what may be on the tracker is what the job says.
            if record.unknown_outcomes:
                raise _unknown(record.unknown_outcomes) from err
            raise
    if record.unknown_outcomes:
        raise _unknown(record.unknown_outcomes)
    return {"folder": os.path.basename(path), "trackers": list(trackers), "uploads": record.uploads}


async def tag(params: TagParams) -> dict[str, Any]:
    """What ``salmon tag`` runs, its questions asked in the browser.

    Returns:
        The folder as it ends up: its new name after a rename, and for a library album the copy in download_directory.
    """
    from salmon.tagger import TagOptions, run_tag

    path = _album(params.path)
    encoding = TAG_ENCODINGS[params.encoding] if params.encoding else None
    options = TagOptions(**{**msgspec.structs.asdict(params), "path": path, "encoding": encoding})
    return {"folder": await run_tag(options)}


async def _run_up(options: "UpOptions") -> None:
    from salmon.uploader import run_up

    try:
        await run_up(options)
    except* DryRunRefused as refused:
        raise click.ClickException(str(refused.exceptions[0])) from None


def _unknown(errors: list[UnknownOutcomeError]) -> UnknownOutcomeError:
    return UnknownOutcomeError("; ".join(str(error) for error in errors))


async def connection_check(params: ConnectionCheckParams) -> dict[str, Any]:
    """``salmon checkconf -t`` for each tracker: what each found, and when. The request dumps are not on.

    Changes nothing. Sends at most two requests to each tracker, one tracker after the other.
    """
    found: list[dict[str, Any]] = []
    for code in params.trackers:
        print_tracker_header(code)
        api = salmon.trackers.get_class(code)()
        try:
            print_session_cookie_header()
            result = await check_connection(code, api, print_step)
        finally:
            await api.close()
        if result.tls_error is not None:
            print_certificate_failure(result)
        print_verdict(result)
        found.append({**msgspec.to_builtins(result), "ok": result.ok, "checked_at": datetime.now(UTC).isoformat()})
    return {"trackers": found}


def _title(what: str) -> Callable[[Any], str]:
    return lambda params: f"{what}: {os.path.basename(params.path)}"


def _checks_title(params: ChecksParams) -> str:
    against = f" against {', '.join(params.trackers)}" if params.trackers else ""
    return f"Checks{against}: {os.path.basename(params.path)}"


def _transcode_title(params: TranscodeParams) -> str:
    return f"Transcode {params.bitrate}: {os.path.basename(params.path)}"


def _connection_check_title(params: ConnectionCheckParams) -> str:
    return f"Connection check: {', '.join(params.trackers)}"


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
        title=_checks_title,
        folder=lambda params: params.path,
        check=_checked_checks,
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
register(
    JobKind(
        name="tag",
        params=TagParams,
        run=tag,
        title=_title("Tag"),
        folder=lambda params: params.path,
        check=_checked_tag,
    )
)
register(
    JobKind(
        name="transcode",
        params=TranscodeParams,
        run=transcode,
        title=_transcode_title,
        folder=lambda params: params.path,
        check=_checked_conversion,
    )
)
register(
    JobKind(
        name="downconvert",
        params=DownconvertParams,
        run=downconvert,
        title=_title("Downconvert"),
        folder=lambda params: params.path,
        check=_checked_conversion,
    )
)
register(
    JobKind(
        name="compress",
        params=CompressParams,
        run=compress,
        title=_title("Recompress"),
        folder=lambda params: params.path,
        check=_checked_compress,
    )
)
register(
    JobKind(
        name="connection_check",
        params=ConnectionCheckParams,
        run=connection_check,
        title=_connection_check_title,
        check=_checked_connection_check,
        exclusive=True,
    )
)
