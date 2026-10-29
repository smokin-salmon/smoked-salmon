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
album under download_directory), a recompression writes in place and is refused for a library album or an album
sharing files through a link (``rewrite_refusal``). A tag job runs what ``salmon tag`` runs (``tagger.run_tag``), its
questions asked in the browser: it sends nothing to a tracker, and tags such an album as a copy renamed into
download_directory, never in place. A cross-upload runs what ``salmon
cross-upload`` runs (``cross_upload.run_cross_upload``): it reads SOURCE and the album folder, and sends TARGET what
the command sends for the same releases and answers, through the same request loop; it writes where the command
writes, never into the album. Each folder is checked by ``paths.album_folder`` before the job is queued, and again
when it starts, since it may have changed while the job waited its turn.

The description generator is ``salmon descgen`` for at most 10 release URLs, each matched against the scrapers before
the job is queued; it scrapes the stores behind the guard for web jobs (``salmon.ssrf``) and returns the BBCode, never
touching the server's clipboard. An image upload is ``salmon images up``: at most 20 files, each read before the job is
queued and again when it starts, and sent only if it is an image by its content (``paths.image_file``), the host chosen
by the command's rules, with a host whose images only display for some trackers (RED's) refused unless a tracker it
displays on is named. A spectrals upload is ``salmon specs``' upload of a finished spectrals job's own images, to the
spectrals host of the tracker picked, ``spectrals_refusal`` honoured. Image hosts are public: nothing but an image
goes there, and a job never reads a file a request names for it outside what ``image_file`` lets through.

The connection check is ``salmon checkconf -t`` for the trackers asked for: at most two requests each (the index
call with the session cookie, then with the API key when one is set), through salmon web's request loop, and only
when the user starts it, never on page load. Metadata sources and seedboxes stay with the command.

Ported from the fork's ``routers/spectrals.py``, ``routers/checks.py`` (chodeus, d6ac6372, 9bfdddc3 and 889cc4f5),
``routers/upload.py`` (styx-techno 637ee666, chodeus 0b29d2d5 and 6f846b37), ``routers/convert.py`` (chodeus
0b29d2d5, a70a3d75, d8c68c28 and a18e0e25), ``routers/tools.py`` (chodeus 0b29d2d5, 9bfdddc3, 1f8e47d8 and 18fc08d9),
``checks/connection.py`` (chodeus, 0b29d2d5, 9bfdddc3 and 75e5a3d9), without its run on load and its cache, and the
cross-upload of ``routers/tools.py`` (chodeus 0b29d2d5, 9bfdddc3 and 3c69045a). The fork's jobs called the commands'
steps, or their click callbacks, themselves; the jobs here run the commands' own code. Unlike the fork's, an image
upload takes images only, not any regular file inside the roots.
"""

import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

import aiohttp
import anyio.to_thread
import asyncclick as click
import msgspec

import salmon.trackers
from salmon import cfg, interaction
from salmon.commands import descgen_refusals
from salmon.config.image_hosts import display_refusal, spectrals_refusal
from salmon.config.validations import canonical_conversions
from salmon.constants import SOURCES, TAG_ENCODINGS
from salmon.converter.transcoding import Bitrate
from salmon.errors import DryRunRefused, ImageUploadFailed, ScrapeError, UnknownOutcomeError
from salmon.images.files import NotAnImage, check_image
from salmon.trackers.connection import (
    check_connection,
    print_certificate_failure,
    print_session_cookie_header,
    print_step,
    print_tracker_header,
    print_verdict,
)
from salmon.webui import paths
from salmon.webui.jobs import (
    FINISHED,
    JobError,
    JobKind,
    PartialResult,
    claim_folder,
    find_job,
    own_folder,
    own_folders,
    register,
)

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


class ConnectionCheckParams(msgspec.Struct, forbid_unknown_fields=True):
    # checkconf's -t: the trackers to check, in order. None: every tracker in the config.
    trackers: list[str] = []


# What a descgen job and an image upload take at most: each URL is a scrape, each file an upload to a public host.
MAX_DESCGEN_URLS = 10
MAX_IMAGE_FILES = 20
MAX_URL_LENGTH = 2000


class DescgenParams(msgspec.Struct, forbid_unknown_fields=True):
    """``salmon descgen``: the release URLs."""

    urls: list[str]


class ImageUploadParams(msgspec.Struct, forbid_unknown_fields=True):
    """``salmon images up``: the files, -i and -t."""

    paths: list[str]
    host: str | None = None
    tracker: str | None = None


class SpectralsUploadParams(msgspec.Struct, forbid_unknown_fields=True):
    """The spectrals of a finished spectrals job, for the tracker whose spectrals host they go to (None: the shared
    [image] specs_uploader, as ``salmon specs`` uses)."""

    job_id: str
    tracker: str | None = None


# Track numbers count from 1: the command's -sp also takes 0 and negative numbers, which pick other tracks.
TrackNumber = Annotated[int, msgspec.Meta(gt=0)]


class UploadParams(msgspec.Struct, forbid_unknown_fields=True):
    """The options of ``salmon up`` a form gives. -yyy and --dry-run are the job's own (``assume_defaults``,
    ``dry_run``)."""

    path: str
    # -s, which the command requires.
    source: str
    # -t, in order, at least one: a web upload never asks which tracker, and offers no other.
    trackers: list[str] = []
    group_id: Annotated[int, msgspec.Meta(gt=0)] | None = None
    skip_flac_upload: bool = False
    request: str | None = None
    source_url: str | None = None
    # --skip-search: take the metadata from source_url without searching the stores.
    skip_search: bool = False
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
    # --conversions: the kinds made after the FLAC, without asking. None follows upload.conversions; [] makes none.
    conversions: list[str] | None = None


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
        "trackers_named": not isinstance(trackers, salmon.trackers.UnnamedTrackers),
        "spectrals": tuple(params.spectrals),
        "encoding": TAG_ENCODINGS[params.encoding] if params.encoding else None,
        "conversions": None if params.conversions is None else tuple(params.conversions),
    }


def _checked_upload(params: UploadParams, dry_run: bool) -> UploadParams:
    """The upload's options as the command's callbacks give them, refused where the command refuses them.

    A tracker the config does not have is refused here, where the command would ask for another in the run, and so
    is an upload with no tracker: the ticked trackers are final, so the run never asks which tracker.
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
    if not trackers:
        raise JobError(400, "Pick at least one tracker.")
    conversions: list[str] | None = None
    if params.conversions is not None:
        try:
            conversions = list(canonical_conversions(params.conversions))
        except ValueError as e:
            raise JobError(400, str(e)) from None
    checked = msgspec.structs.replace(
        params,
        path=paths.album_folder(params.path),
        source=source,
        encoding=encoding,
        trackers=trackers,
        conversions=conversions,
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


def _checked_descgen(params: DescgenParams, _dry_run: bool) -> DescgenParams:
    """The URLs descgen takes: at most ``MAX_DESCGEN_URLS``, each one a release URL a scraper takes, or the job is
    refused (422) before any request, naming the URL as the command does."""
    urls = [url.strip() for url in params.urls]
    if not urls:
        raise JobError(422, "Give at least one release URL.")
    if len(urls) > MAX_DESCGEN_URLS:
        raise JobError(422, f"Give at most {MAX_DESCGEN_URLS} release URLs: {len(urls)} were given.")
    if any(len(url) > MAX_URL_LENGTH for url in urls):
        raise JobError(422, f"A release URL is at most {MAX_URL_LENGTH} characters.")
    # Never whether a URL is a folder here: that would tell a browser what exists outside the folders it may see.
    if refusals := descgen_refusals(urls, folders=False):
        raise JobError(422, " ".join(refusals))
    return msgspec.structs.replace(params, urls=urls)


def _image_host(params: ImageUploadParams) -> tuple[str, str | None]:
    """The host and the tracker an image upload goes to, by the rules of ``salmon images up``.

    With a tracker, ``image_host_for_tracker`` decides: the host named (which must display there) else the tracker's
    image_uploader. Without one, the host named else [image] image_uploader, and a host whose images only display for
    some trackers' members (RED's) is refused: nothing says which tracker it is for (ADR 0002).

    Raises:
        JobError: An unknown host or tracker, or a host that cannot be used for the tracker.
    """
    from salmon import images

    if params.host is not None and params.host not in images.HOSTS:
        raise JobError(422, f"{params.host} is not a valid image host.")
    if params.tracker:
        code = params.tracker.strip().upper()
        if code not in salmon.trackers.tracker_list:
            raise JobError(422, f"{params.tracker} is not a tracker in your config.")
        try:
            return images.image_host_for_tracker(code, params.host), code
        except images.ImageHostRefused as e:
            raise JobError(422, str(e)) from None
    host = params.host or cfg.image.image_uploader
    if (reason := display_refusal(host)) is not None:
        raise JobError(422, f"{host} can't be used without a tracker: {reason}. Name the tracker the images are for.")
    return host, None


def _image_count(params: ImageUploadParams) -> None:
    if not params.paths:
        raise JobError(422, "Pick at least one image.")
    if len(params.paths) > MAX_IMAGE_FILES:
        raise JobError(422, f"Pick at most {MAX_IMAGE_FILES} images: {len(params.paths)} were picked.")


def _preflight_images(params: ImageUploadParams) -> None:
    """In a worker thread: every file read to see it is an image, before the job is queued."""
    _image_count(params)
    for raw in params.paths:
        paths.image_file(raw)


def _checked_images(params: ImageUploadParams, dry_run: bool) -> ImageUploadParams:
    """The files (each inside a root, a regular file, not a symlink, not in a folder of .torrent files or a
    seedbox's; ``preflight`` read them), the host and the tracker, as ``salmon images up`` takes them."""
    # Every image host's upload refuses in a dry run.
    if dry_run:
        raise JobError(400, "An image upload has no dry run: it sends the files to an image host.")
    _image_count(params)
    host, tracker = _image_host(params)
    # Once each, in the order given.
    resolved = list(dict.fromkeys(paths.image_file(raw, content=False) for raw in params.paths))
    return msgspec.structs.replace(params, paths=resolved, host=host, tracker=tracker)


def _spectrals_host(tracker: str | None) -> tuple[str, str | None]:
    """The spectrals host of a tracker (None: the shared [image] setting) and the tracker as the config knows it.

    Raises:
        JobError: An unknown tracker, or a host the spectrals rules refuse (the config does not load with one, but
            a request is not to rely on that).
    """
    code = tracker.strip().upper() if tracker else None
    if code is not None and code not in salmon.trackers.tracker_list:
        raise JobError(422, f"{tracker} is not a tracker in your config.")
    host = cfg.image.host_for(code, "specs_uploader")
    if (reason := spectrals_refusal(host, code)) is not None:
        raise JobError(422, f"{host} can't be used for spectrals: {reason}.")
    return host, code


def _spectrals_source(job_id: str) -> tuple[str, dict[int, str]]:
    """The folder and the tracks of a finished spectrals job whose images are still kept: the job's own folder,
    which only a spectrals job makes, never a path from a request.

    Raises:
        JobError: There is no such spectrals job (404), or it has not finished, failed, or was discarded (409).
    """
    job = find_job(job_id)
    if job is None or job.kind.name != "spectrals":
        raise JobError(404, "No such spectrals job.")
    if job.status not in FINISHED:
        raise JobError(409, "This spectrals job has not finished: answer its question or wait for it to end.")
    folders = own_folders(job)
    tracks = (job.result or {}).get("tracks") if isinstance(job.result, dict) else None
    if job.status != "done" or not tracks:
        raise JobError(409, "This spectrals job did not make spectrals.")
    if job.spectrals is None or len(folders) != 1:
        raise JobError(409, "The spectrals of this job were discarded.")
    return folders[0], {int(spectral_id): name for spectral_id, name in tracks.items()}


def _checked_spectrals_upload(params: SpectralsUploadParams, _dry_run: bool) -> SpectralsUploadParams:
    _spectrals_source(params.job_id)
    _host, tracker = _spectrals_host(params.tracker)
    return msgspec.structs.replace(params, tracker=tracker)


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
    """What ``salmon compress`` runs: the album's FLACs recompressed in place, unless ``compress_refusal`` says no."""
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
    # The command's -t: none given takes the config's default_trackers, else asks which tracker.
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


async def descgen(params: DescgenParams) -> dict[str, Any]:
    """What ``salmon descgen`` makes: the BBCode description of the releases at the URLs. The clipboard is the
    browser's to fill, never the server's.

    Fetches each URL once, from the stores, behind the guard that keeps web jobs to public addresses.
    """
    from salmon.commands import build_description

    # Again: the URLs are ones a scraper takes, whatever happened while the job waited.
    if refusals := descgen_refusals(params.urls, folders=False):
        raise click.ClickException(" ".join(refusals))
    try:
        description = await build_description(params.urls)
    except ScrapeError as e:
        raise click.ClickException(f"Scrape failed: {e}") from None
    except (aiohttp.ClientError, TimeoutError) as e:
        # aiohttp repeats the request URL: name the error only.
        raise click.ClickException(f"Scrape failed ({type(e).__name__}).") from None
    return {"description": description}


def _images_refused(error: JobError) -> click.ClickException:
    return click.ClickException(error.detail)


async def upload_images(params: ImageUploadParams) -> dict[str, Any]:
    """What ``salmon images up`` runs: the files uploaded to the host, over the command's connections.

    Every file is read again first, since it may have changed while the job waited. Nothing else is sent, and
    the clipboard is the browser's to fill, never the server's.

    Returns:
        The host, the tracker, and each file with its URL.
    """
    from salmon import images

    try:
        host, tracker = _image_host(params)
        checked = await anyio.to_thread.run_sync(lambda: [paths.image_file(raw) for raw in params.paths])
    except JobError as e:
        raise _images_refused(e) from None
    try:
        urls = await images.upload_images(tuple(checked), images.HOSTS[host], copy_to_clipboard=False)
    except ImageUploadFailed as e:
        raise click.ClickException(f"{e}: {e.__cause__}" if e.__cause__ is not None else str(e)) from None
    return {
        "host": host,
        "tracker": tracker,
        "images": [{"file": os.path.basename(path), "url": url} for path, url in zip(checked, urls, strict=True)],
    }


def _spectral_images(folder: str, tracks: dict[int, str]) -> None:
    """Each image of the tracks, in the job's own folder, read to see it is the image it should be.

    Raises:
        click.ClickException: A file is missing, is a symlink or is not an image.
    """
    for spectral_id in tracks:
        for kind in ("Full", "Zoom"):
            name = f"{spectral_id:02d} {kind}.png"
            path = os.path.join(folder, name)
            try:
                if os.path.realpath(path) != path or not stat.S_ISREG(os.lstat(path).st_mode):
                    raise click.ClickException(f"{name} is gone from the job's folder, or no longer a plain file.")
                check_image(path)
            except FileNotFoundError:
                raise click.ClickException(f"{name} is gone from the job's folder.") from None
            except NotAnImage as e:
                raise click.ClickException(f"{name} is not an image any more ({e}).") from None


async def upload_spectrals(params: SpectralsUploadParams) -> dict[str, Any]:
    """What ``salmon specs`` uploads, from a finished spectrals job: its images to the spectrals host of the tracker,
    from the job's own folder only. A track that fails is offered to another host, as the command does.

    Returns:
        The host and the tracker, each track's URLs, and the text ``salmon specs`` prints (plain, and as BBCode).
    """
    from salmon.commands import format_spectral_urls
    from salmon.uploader.spectrals import upload_spectrals as upload_spectral_images

    try:
        folder, tracks = _spectrals_source(params.job_id)
        host, tracker = _spectrals_host(params.tracker)
    except JobError as e:
        raise click.ClickException(e.detail) from None
    await anyio.to_thread.run_sync(_spectral_images, folder, tracks)
    uploaded = await upload_spectral_images(folder, tracks, tracker)
    if not uploaded:
        raise click.ClickException("No spectral was uploaded.")
    filenames = [tracks.get(number, "") for number in range(1, max(tracks) + 1)]
    return {
        "host": host,
        "tracker": tracker,
        "tracks": [
            {"id": spectral_id, "track": tracks[spectral_id], "urls": urls} for spectral_id, urls in uploaded.items()
        ],
        "text": format_spectral_urls(filenames, uploaded, False),
        "bbcode": format_spectral_urls(filenames, uploaded, True),
    }


def _title(what: str) -> Callable[[Any], str]:
    return lambda params: f"{what}: {os.path.basename(params.path)}"


def _checks_title(params: ChecksParams) -> str:
    against = f" against {', '.join(params.trackers)}" if params.trackers else ""
    return f"Checks{against}: {os.path.basename(params.path)}"


def _cross_upload_title(params: CrossUploadParams) -> str:
    return f"Cross-upload {params.source} to {params.target}: {', '.join(params.inputs)}"


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
register(
    JobKind(
        name="descgen",
        params=DescgenParams,
        run=descgen,
        title=lambda params: f"Description from {len(params.urls)} URL(s)",
        check=_checked_descgen,
    )
)
register(
    JobKind(
        name="images",
        params=ImageUploadParams,
        run=upload_images,
        title=lambda params: f"Upload {len(params.paths)} image(s) to {params.host or 'the image host'}",
        check=_checked_images,
        preflight=_preflight_images,
    )
)
register(
    JobKind(
        name="spectrals_upload",
        params=SpectralsUploadParams,
        run=upload_spectrals,
        title=lambda params: (
            f"Upload spectrals of {params.job_id}" + (f" for {params.tracker}" if params.tracker else "")
        ),
        check=_checked_spectrals_upload,
    )
)
