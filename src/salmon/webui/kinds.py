"""The jobs salmon web runs on an album folder: spectrals, file checks, uploads and conversions (ADR 0004, section 5).

Spectrals send nothing anywhere: no tracker request, no image host. Checks send nothing either unless trackers are
named: then each named tracker gets what ``salmon check all -t`` sends it, salmon up's dupe search (the index call,
then one browse per search string), through salmon web's request loop. Both read the album folder only, which may
be in library_dirs. An upload runs what ``salmon up`` runs (``uploader.run_up``), its questions asked in the browser:
it sends what the command sends for the same album and answers, every tracker request through salmon web's request
loop (``trackers.account``), and works on a copy of a library album, as the command does. A conversion (transcode,
downconvert) or a recompression (compress) runs what ``salmon transcode``, ``salmon downconv`` and ``salmon compress``
run and sends nothing anywhere: a conversion writes where the command puts it (beside the album, or for a library
album under download_directory), a recompression writes in place and is refused for a library album. A cross-upload
runs what ``salmon cross-upload`` runs (``cross_upload.run_cross_upload``): it reads SOURCE and the album folder, and
sends TARGET what the command sends for the same releases and answers, through the same request loop; it writes
where the command writes, never into the album. Each folder is checked by ``paths.album_folder`` before the job is
queued, and again when it starts, since it may have changed while the job waited its turn.

Ported from the fork's ``routers/spectrals.py``, ``routers/checks.py`` (chodeus, d6ac6372, 9bfdddc3 and 889cc4f5),
``routers/upload.py`` (styx-techno 637ee666, chodeus 0b29d2d5 and 6f846b37), ``routers/convert.py`` (chodeus
0b29d2d5, a70a3d75, d8c68c28 and a18e0e25) and the cross-upload of ``routers/tools.py`` (chodeus 0b29d2d5, 9bfdddc3
and 3c69045a), without the spectrals upload. The fork's jobs called the commands' steps, or their click callbacks,
themselves; the jobs here run the commands' own code.
"""

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

import anyio.to_thread
import asyncclick as click
import msgspec

import salmon.trackers
from salmon import interaction
from salmon.constants import SOURCES, TAG_ENCODINGS
from salmon.converter.transcoding import Bitrate
from salmon.errors import DryRunRefused, UnknownOutcomeError
from salmon.webui import paths
from salmon.webui.jobs import JobError, JobKind, PartialResult, claim_folder, own_folder, register

if TYPE_CHECKING:
    from pathlib import Path

    from salmon.cross_upload import CrossUploadOptions
    from salmon.uploader import UpOptions
    from salmon.uploader.record import RunRecord


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


class CrossUploadParams(msgspec.Struct, forbid_unknown_fields=True):
    """The arguments and options of ``salmon cross-upload`` a form gives. -yyy and --dry-run are the job's own
    (``assume_defaults``, ``dry_run``)."""

    # INPUT...: SOURCE torrent IDs or torrent URLs, kept as the IDs they name. A browser names no .torrent file.
    inputs: list[str]
    # SOURCE_TRACKER and TARGET_TRACKER.
    source: str
    target: str
    # --path: the album folder, when it is not download_directory/<the torrent's folder>.
    path: str | None = None
    group_id: Annotated[int, msgspec.Meta(gt=0)] | None = None
    # --transcode, each bitrate once.
    transcodes: list[str] = []
    downconvert: bool = False
    # --all
    all_formats: bool = False


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


def _cross_upload_options(params: CrossUploadParams, path: str | None) -> "CrossUploadOptions":
    from salmon.cross_upload import CrossUploadOptions

    return CrossUploadOptions(
        inputs=tuple(params.inputs),
        source=params.source,
        target=params.target,
        path=path,
        group_id=params.group_id,
        transcodes=tuple(params.transcodes),
        downconvert=params.downconvert,
        all_formats=params.all_formats,
    )


def _checked_cross_upload(params: CrossUploadParams, _dry_run: bool) -> CrossUploadParams:
    """The cross-upload's arguments as the command parses them, refused where it refuses them, before any request.

    Each INPUT is kept as the torrent ID it names: a URL is neither shown nor kept.
    """
    from salmon.cross_upload import TRANSCODES, is_torrent_reference, torrent_id_of

    transcodes: list[str] = []
    for bitrate in params.transcodes:
        code = bitrate.strip().upper()
        if code not in TRANSCODES:
            raise JobError(400, f"{bitrate} is not a transcode salmon makes: {' or '.join(TRANSCODES)}.")
        if code not in transcodes:
            transcodes.append(code)
    checked = msgspec.structs.replace(
        params,
        inputs=[value.strip() for value in params.inputs],
        source=params.source.strip().upper(),
        target=params.target.strip().upper(),
        transcodes=transcodes,
    )
    # A tracker not in the config is refused here: no client of it is made.
    if (error := _cross_upload_options(checked, params.path).usage_error()) is not None:
        raise JobError(400, error)
    source = salmon.trackers.get_class(checked.source)()
    ids: list[str] = []
    for value in checked.inputs:
        # Anything else would be a .torrent file on the server: never looked for.
        if not is_torrent_reference(value):
            raise JobError(400, f"{value} is not a {source.site_string} torrent ID or URL.")
        try:
            ids.append(str(torrent_id_of(value, source)))
        except click.UsageError as e:
            raise JobError(400, e.format_message()) from None
    path = paths.album_folder(params.path) if params.path is not None else None
    return msgspec.structs.replace(checked, inputs=ids, path=path)


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


async def _run_up(options: "UpOptions") -> None:
    from salmon.uploader import run_up

    try:
        await run_up(options)
    except* DryRunRefused as refused:
        raise click.ClickException(str(refused.exceptions[0])) from None


async def cross_upload(params: CrossUploadParams) -> dict[str, Any]:
    """What ``salmon cross-upload`` runs, its questions asked in the browser.

    The job works on one album folder at a time, as every job does: the path given, else each release's folder from
    when SOURCE names it until the job ends (``claim_folder``). A release whose folder another running job works on
    is not cross-uploaded, as one that fails a check is not; a job started on that folder later waits for this one.

    Returns:
        SOURCE, TARGET, the torrent IDs, and each torrent uploaded (none in a dry run).

    Raises:
        PartialResult: The run stopped (a refusal, a failed step, an abort, a cancel), with what it uploaded before.
            The job ends as unknown_outcome when an upload or a lossy report may have reached TARGET, also when the
            run went on after it, and as the command's failure otherwise.
    """
    from salmon.uploader.record import recording

    path = _album(params.path) if params.path is not None else None
    with recording() as record:
        try:
            await _run_cross_upload(_cross_upload_options(params, path))
        except BaseException as err:
            ended = _unknown(record.unknown_outcomes) if record.unknown_outcomes else err
            raise PartialResult(_cross_upload_result(params, record)) from ended
    if record.unknown_outcomes:
        raise PartialResult(_cross_upload_result(params, record)) from _unknown(record.unknown_outcomes)
    return _cross_upload_result(params, record)


async def _run_cross_upload(options: "CrossUploadOptions") -> None:
    from salmon.cross_upload import Stopped, run_cross_upload

    try:
        try:
            await run_cross_upload(options, claim_folder=_claim_album)
        except* DryRunRefused as refused:
            raise click.ClickException(str(refused.exceptions[0])) from None
    except Stopped as stopped:
        # The log says why, as the terminal does; the job's error repeats it.
        raise click.ClickException(stopped.reason) from stopped


async def _claim_album(folder: "Path") -> str | None:
    holder = await claim_folder(str(folder))
    if holder is None:
        return None
    return f"another job works on {folder} ({holder}): cross-upload it once that job has ended"


def _cross_upload_result(params: CrossUploadParams, record: "RunRecord") -> dict[str, Any]:
    return {"source": params.source, "target": params.target, "inputs": params.inputs, "uploads": record.uploads}


def _unknown(errors: list[UnknownOutcomeError]) -> UnknownOutcomeError:
    return UnknownOutcomeError("; ".join(str(error) for error in errors))


def _title(what: str) -> Callable[[Any], str]:
    return lambda params: f"{what}: {os.path.basename(params.path)}"


def _checks_title(params: ChecksParams) -> str:
    against = f" against {', '.join(params.trackers)}" if params.trackers else ""
    return f"Checks{against}: {os.path.basename(params.path)}"


def _cross_upload_title(params: CrossUploadParams) -> str:
    return f"Cross-upload {params.source} to {params.target}: {', '.join(params.inputs)}"


def _transcode_title(params: TranscodeParams) -> str:
    return f"Transcode {params.bitrate}: {os.path.basename(params.path)}"


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
        name="cross_upload",
        params=CrossUploadParams,
        run=cross_upload,
        title=_cross_upload_title,
        folder=lambda params: params.path,
        check=_checked_cross_upload,
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
