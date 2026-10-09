import functools
import os
import platform
import shutil
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import anyio
import asyncclick as click
import pyperclip
from mutagen import MutagenError

import salmon.trackers
from salmon import cfg, dryrun, interaction
from salmon.checks import mqa_test
from salmon.checks.do_not_upload import Candidate, do_not_upload_reason
from salmon.checks.high_rate import sixteen_bit_notice
from salmon.checks.integrity import resolve_integrity_for_upload
from salmon.checks.logs import check_log_cambia
from salmon.checks.provenance import gather_provenance
from salmon.checks.source import DetectedSource, detect_source
from salmon.checks.tag_rules import process_tag_issues
from salmon.checks.upconverts import upload_upconvert_test
from salmon.common import commandgroup, tagify
from salmon.config.image_hosts import cover_refusal
from salmon.constants import ENCODINGS, FORMATS, SOURCES, TAG_ENCODINGS
from salmon.converter.conversions import conversion_of
from salmon.converter.downconverting import (
    BitDepth,
    conversion_note,
    convert_folder,
    generate_conversion_description,
)
from salmon.converter.transcoding import (
    Bitrate,
    generate_transcode_description,
    transcode_folder,
    transcode_note,
)
from salmon.errors import (
    AbortAndDeleteFolder,
    CRCMismatchError,
    DryRunRefused,
    EditedLogError,
    InvalidMetadataError,
    LogCheckSkipped,
    RequestError,
    UnknownOutcomeError,
    UploadError,
)
from salmon.images import HOSTS, upload_cover
from salmon.tagger import (
    metadata_validator_base,
    validate_encoding,
    validate_source,
)
from salmon.tagger.ai_review import review_metadata_with_ai
from salmon.tagger.audio_info import (
    check_hybrid,
    gather_audio_info,
    recompress_path,
)
from salmon.tagger.cover import check_embedded_pictures, compress_pictures, download_cover_if_nonexistent
from salmon.tagger.foldername import rename_folder
from salmon.tagger.folderstructure import check_folder_structure
from salmon.tagger.metadata import get_metadata
from salmon.tagger.pre_data import construct_rls_data
from salmon.tagger.retagger import rename_files, tag_files
from salmon.tagger.review import review_metadata, suggest_release_type
from salmon.tagger.tags import check_tags, gather_tags, standardize_tags
from salmon.trackers.base import TagRules
from salmon.trackers.red import RedApi
from salmon.uploader import record
from salmon.uploader.dupe_checker import (
    can_check_site_log,
    check_existing_group,
    choose_source_flac,
    dupe_check_recent_torrents,
    fetch_existing_group_candidates_in_background,
    generate_dupe_check_searchstrs,
    held_formats,
    print_recent_upload_results,
    print_torrents,
    resolve_existing_group,
)
from salmon.uploader.preassumptions import confirm_group_upload, print_preassumptions, validate_skip_flac_source
from salmon.uploader.request_checker import check_requests
from salmon.uploader.seedbox import UploadManager
from salmon.uploader.spectrals import (
    SpectralUploads,
    check_spectrals,
    generate_lossy_approval_comment,
    post_upload_spectral_check,
    report_lossy_master,
    specs_hosts_text,
)
from salmon.uploader.staging import staged_source
from salmon.uploader.upload import (
    concat_track_data,
    prepare_and_upload,
)

if TYPE_CHECKING:
    from salmon.tagger.tagfile import TagFile
    from salmon.trackers.base import BaseGazelleApi


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option("--group-id", "-g", default=None, help="Group ID to upload torrent to")
@click.option(
    "--skip-flac-upload",
    is_flag=True,
    help="The FLAC is already in --group-id: do not upload it, only upload transcodes of it into that group.",
)
@click.option(
    "--source",
    "-s",
    type=click.STRING,
    callback=validate_source,
    help=f"Source of files ({'/'.join(SOURCES.values())})",
)
@click.option(
    "--lossy/--not-lossy",
    "-l/-L",
    default=None,
    help="Whether or not the files are lossy mastered",
)
@click.option(
    "--spectrals",
    "-sp",
    type=click.INT,
    multiple=True,
    help="Track numbers of spectrals to include in torrent description",
)
@click.option(
    "--overwrite",
    "-ow",
    is_flag=True,
    help="Whether or not to use the original metadata.",
)
@click.option(
    "--encoding",
    "-e",
    type=click.STRING,
    callback=validate_encoding,
    help="You must specify one of the following encodings if files aren't lossless: "
    + ", ".join(list(TAG_ENCODINGS.keys())),
)
@click.option(
    "--compress",
    "-c",
    is_flag=True,
    help="Recompress flacs to the configured compression level before uploading.",
)
@click.option(
    "--tracker",
    "-t",
    "trackers",
    multiple=True,
    callback=salmon.trackers.validate_trackers,
    help=f"Uploading Choices: ({'/'.join(salmon.trackers.tracker_list)}). Name several, comma-separated or "
    "with -t repeated, to upload to each in that order without being asked for another tracker.",
)
@click.option("--request", "-r", default=None, help="Pass a request URL or ID")
@click.option(
    "--spectrals-after",
    "-a",
    is_flag=True,
    help="Assess / upload / report spectrals after torrent upload",
)
@click.option(
    "--auto-rename",
    "-n",
    is_flag=True,
    help="Rename files and folders automatically",
)
@click.option(
    "--skip-up",
    is_flag=True,
    help="Skip check for 24 bit upconversion",
)
@click.option("--scene", is_flag=True, help="Is this a scene release (default: False)")
@click.option(
    "--source-url",
    "-su",
    default=None,
    help="For WEB uploads provide the source of the album to be added in release description",
)
@click.option(
    "--skip-initial-review",
    is_flag=True,
    help="Skip the initial manual metadata review before AI review.",
)
@click.option(
    "--apply-ai-suggestions",
    is_flag=True,
    help="Automatically apply AI review suggestions when AI review is enabled.",
)
@click.option("-yyy", is_flag=True, help="Automatically pick the default answer for prompt")
@click.option(
    "--skip-mqa",
    is_flag=True,
    help="Skip the check for an MQA marker",
)
@click.option(
    "--skip-log-check",
    is_flag=True,
    help="Skip checking CD logs",
)
@click.option(
    "--skip-integrity-check",
    is_flag=True,
    help="Skip integrity check of audio files",
)
@click.option(
    "--essential-only",
    "-eo",
    is_flag=True,
    help="Only keep essential files; strip nfo, sfv, md5, txt, and other extras.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Go through the whole upload on a copy of the album and send nothing: print what each upload would "
    "send instead. Nothing is posted to a tracker or uploaded to an image host, copied to a seedbox or added "
    "to a torrent client.",
)
async def up(
    path: str,
    group_id: int | None,
    skip_flac_upload: bool,
    source: str | None,
    lossy: bool | None,
    spectrals: tuple[int, ...],
    overwrite: bool,
    encoding: str | None,
    compress: bool,
    trackers: tuple[str, ...],
    request: str | None,
    spectrals_after: bool,
    auto_rename: bool,
    skip_up: bool,
    scene: bool,
    source_url: str | None,
    skip_initial_review: bool,
    apply_ai_suggestions: bool,
    yyy: bool,
    skip_mqa: bool,
    skip_log_check: bool,
    skip_integrity_check: bool,
    essential_only: bool,
    dry_run: bool,
) -> None:
    """Command to upload an album folder to a Gazelle Site."""
    options = UpOptions(
        path=path,
        trackers=trackers,
        source=source,
        group_id=group_id,
        skip_flac_upload=skip_flac_upload,
        lossy=lossy,
        spectrals=spectrals,
        overwrite=overwrite,
        encoding=encoding,
        compress=compress,
        request=request,
        spectrals_after=spectrals_after,
        auto_rename=auto_rename,
        skip_up=skip_up,
        scene=scene,
        source_url=source_url,
        skip_initial_review=skip_initial_review,
        apply_ai_suggestions=apply_ai_suggestions,
        skip_mqa=skip_mqa,
        skip_log_check=skip_log_check,
        skip_integrity_check=skip_integrity_check,
        essential_only=essential_only,
    )
    if (error := options.usage_error(dry_run)) is not None:
        raise click.UsageError(error)
    with dryrun.mode(dry_run), interaction.assuming_defaults(yyy):
        try:
            await run_up(options)
        except* DryRunRefused as refused:
            # except*: a refusal in a task group comes out in an exception group.
            click.secho(f"\n{refused.exceptions[0]}", fg="red", bold=True)
            raise click.exceptions.Exit(1) from refused


@dataclass(frozen=True, kw_only=True)
class UpOptions:
    """What ``salmon up`` uploads, and how: its options once parsed, but -yyy and --dry-run, which are the run's.

    salmon web's upload job builds the same from its form, and runs the same ``run_up``.

    Attributes:
        trackers: The site codes to upload to, in order, chosen and checked already (``validate_trackers``).
        source: The media source, as ``validate_source`` gives it.
        group_id: The group to upload into.
        encoding: The lossy encoding, as ``validate_encoding`` gives it.
        request: The request to fill, a URL or an ID, checked against the tracker by ``run_up``.
    """

    path: str
    trackers: tuple[str, ...]
    source: str | None
    group_id: int | None = None
    skip_flac_upload: bool = False
    lossy: bool | None = None
    spectrals: tuple[int, ...] = ()
    overwrite: bool = False
    encoding: str | None = None
    compress: bool = False
    request: str | None = None
    spectrals_after: bool = False
    auto_rename: bool = False
    skip_up: bool = False
    scene: bool = False
    source_url: str | None = None
    skip_initial_review: bool = False
    apply_ai_suggestions: bool = False
    skip_mqa: bool = False
    skip_log_check: bool = False
    skip_integrity_check: bool = False
    essential_only: bool = False

    def usage_error(self, dry_run: bool) -> str | None:
        """Why these options cannot go together, as ``salmon up`` says it; None when they can."""
        if self.skip_flac_upload and self.group_id is None:
            return "--skip-flac-upload requires --group-id."
        if self.skip_flac_upload and self.request:
            return "--skip-flac-upload cannot be used with --request."
        if self.skip_flac_upload and self.spectrals_after:
            return "--skip-flac-upload cannot be used with --spectrals-after."
        if self.essential_only and self.scene:
            return "--essential-only and --scene cannot be used together."
        if self.skip_flac_upload and self.trackers[1:]:
            return "--skip-flac-upload uploads to one tracker: give -t a single tracker."
        if dry_run and self.spectrals_after:
            return (
                "--dry-run cannot be used with --spectrals-after: that step edits the uploaded torrent, and a dry run "
                "uploads none."
            )
        return None


async def run_up(options: UpOptions) -> None:
    """Upload an album folder as ``salmon up`` does, with the options checked by ``UpOptions.usage_error``.

    A dry run and -yyy are the caller's, set around the call (``dryrun.mode``, ``interaction.assuming_defaults``).

    Raises:
        DryRunRefused: A step that sends something ran in a dry run, maybe in an exception group.
    """
    path = options.path
    if dryrun.active():
        dryrun.say("the upload runs on a copy of the album and sends nothing. Each upload's form is printed instead.")
    gazelle_site = salmon.trackers.get_class(options.trackers[0])()
    request = options.request
    if request:
        request = salmon.trackers.validate_request(gazelle_site, request)
        # This is isn't handled by click because we need the tracker sorted first.
    print_preassumptions(
        gazelle_site,
        path,
        options.group_id,
        options.source,
        options.lossy,
        options.spectrals,
        options.encoding,
        options.spectrals_after,
    )
    flac_group = None
    if options.group_id:
        group = await confirm_group_upload(gazelle_site, options.group_id, options.source)
        if options.skip_flac_upload:
            flac_group = group
    source_url = options.source_url.strip() if options.source_url else options.source_url
    await upload(
        gazelle_site,
        path,
        options.group_id,
        options.source,
        options.lossy,
        options.spectrals,
        options.encoding,
        source_url=source_url,
        scene=options.scene,
        overwrite_meta=options.overwrite,
        recompress=options.compress,
        request_id=request,
        spectrals_after=options.spectrals_after,
        auto_rename=options.auto_rename,
        skip_up=options.skip_up,
        skip_mqa=options.skip_mqa,
        skip_log_check=options.skip_log_check,
        skip_integrity_check=options.skip_integrity_check,
        essential_only=options.essential_only,
        flac_group=flac_group,
        skip_initial_review=options.skip_initial_review,
        apply_ai_suggestions=options.apply_ai_suggestions,
        trackers=list(options.trackers) if len(options.trackers) > 1 else None,
    )
    if dryrun.active():
        dryrun.say(f"done. Nothing was sent, and {path} is unchanged.")


async def get_cover_url(
    tracker: str,
    cover_urls: dict[str, str | None],
    path: str,
    cover_source: str | None,
    remove_downloaded: bool,
    red_api: RedApi | None = None,
    host: str | None = None,
) -> tuple[str | None, bool]:
    """Get the cover URL for a new group on a tracker, uploading the cover if needed.

    Each tracker can have its own cover host, so a cover uploaded for one tracker is
    only reused by trackers that share its host. A failed upload is retried next time.

    Args:
        tracker: The tracker site code, e.g. "RED".
        cover_urls: Cover URLs already uploaded in this run, by image host. Updated in place.
        path: The release folder.
        cover_source: URL to download the cover from if the folder has none.
        remove_downloaded: Delete the cover file after uploading, if it was downloaded.
        red_api: The RED client that RED's image host uploads through.
        host: Upload to this host instead of the tracker's configured one, for a retry on
            another host. The URL is still cached in cover_urls under this host.

    Returns:
        The cover URL (None if none is cached and the upload failed or no cover was found), and
        whether a cover file was found: False means there is nothing to retry uploading, True
        with a None URL means the upload itself failed. A dry run uploads nothing: the URL is
        what stands in for it.
    """
    host = host or cfg.image.host_for(tracker, "cover_uploader")
    if not cover_urls.get(host):
        cover_path, is_downloaded = await download_cover_if_nonexistent(path, cover_source)
        if dryrun.active() and cover_path:
            dryrun.say(f"not uploading the cover {os.path.basename(cover_path)} to {host}.")
            cover_urls[host] = dryrun.image_url(cover_path, host)
        else:
            cover_urls[host] = await upload_cover(cover_path, host, red_api)
        if is_downloaded and remove_downloaded and cover_path:
            click.secho("Removing downloaded Cover Image File", fg="yellow")
            os.remove(cover_path)
        return cover_urls[host], cover_path is not None
    return cover_urls[host], True


@asynccontextmanager
async def red_api_for_covers(gazelle_site: "BaseGazelleApi", host: str | None = None) -> AsyncIterator[RedApi | None]:
    """Get the RED client that a cover upload to `host` for gazelle_site's tracker goes through, if any.

    RED's image host authenticates with the RED API key, whichever tracker the cover is for. An
    upload to RED uses its own client. An upload to another tracker gets one RED client of its
    own, closed once the upload it was made for is done.

    Args:
        gazelle_site: The tracker API instance the upload is to.
        host: The image host the cover is going to. Defaults to the tracker's configured cover host.

    Yields:
        The RED client, or None if the cover is not going to RED's image host.
    """
    if isinstance(gazelle_site, RedApi):
        yield gazelle_site
    elif (host if host is not None else cfg.image.host_for(gazelle_site.site_code, "cover_uploader")) == "red":
        red_api = RedApi()
        try:
            yield red_api
        finally:
            await red_api.close()
    else:
        yield None


async def _choose_cover_host(tracker: str, default_host: str) -> str:
    """Ask which image host to retry a failed cover upload with.

    Args:
        tracker: The tracker site code the cover is for, e.g. "RED".
        default_host: The host offered as the default answer.

    Returns:
        The chosen host, valid as a cover host for tracker.
    """
    while True:
        forbidden = {host: reason for host in HOSTS if (reason := cover_refusal(host, tracker)) is not None}
        allowed_hosts = [host for host in HOSTS if host not in forbidden]
        host_input: str = await interaction.prompt(
            click.style(
                "Which image host would you like to retry the cover upload with? "
                f"(Options: {', '.join(allowed_hosts)})",
                fg="magenta",
                bold=True,
            ),
            default=default_host,
        )
        host = host_input.strip().lower()
        if host in forbidden:
            click.secho(f"{host} can't be used as a cover host for {tracker}: {forbidden[host]}.", fg="red")
        elif host not in HOSTS:
            click.secho(f"{host} is an invalid image host. Please choose another one.", fg="red")
        else:
            return host


async def resolve_cover_url(
    gazelle_site: "BaseGazelleApi",
    group_id: int | None,
    cover_urls: dict[str, str | None],
    path: str,
    cover_source: str | None,
    remove_downloaded: bool,
) -> tuple[bool, str | None]:
    """Get the cover URL to upload to a tracker with, asking before a new group goes up without one.

    An existing group already has its cover, so it needs none. For a new group with no cover,
    --yes-all stops the upload; otherwise the user can go on without one, retry, or stop. A retry
    after a failed upload (a cover file was found) asks which host to retry with; a retry with no
    cover found just looks at the folder again.

    Args:
        gazelle_site: The tracker API instance the upload is to.
        group_id: The existing group to upload to, or None for a new group.
        cover_urls: Cover URLs already uploaded in this run, by image host. Updated in place.
        path: The release folder.
        cover_source: URL to download the cover from if the folder has none.
        remove_downloaded: Delete the cover file after uploading, if it was downloaded.

    Returns:
        Whether to upload to this tracker, and the cover URL to upload with (None for none).
    """
    tracker = gazelle_site.site_code
    if group_id:
        if not remove_downloaded:
            await download_cover_if_nonexistent(path, cover_source)
        return True, None

    default_host = cfg.image.host_for(tracker, "cover_uploader")
    host = default_host
    async with AsyncExitStack() as stack:
        red_api: RedApi | None = None
        while True:
            if host == "red" and red_api is None:
                red_api = await stack.enter_async_context(red_api_for_covers(gazelle_site, host))
            cover_url, cover_found = await get_cover_url(
                tracker, cover_urls, path, cover_source, remove_downloaded, red_api if host == "red" else None, host
            )
            if cover_url:
                return True, cover_url

            click.secho(
                f"\nNo cover image for this new group on {tracker}: none was found, or the upload to {host} failed.",
                fg="yellow",
                bold=True,
            )
            if await interaction.assume_defaults():
                click.secho("Not uploading a new group without a cover image with --yes-all.", fg="red", bold=True)
                return False, None

            choice = await interaction.prompt(
                click.style("Continue without a cover image? [y/N/r]", fg="magenta"),
                default="n",
                show_default=False,
            )
            choice = choice.strip().lower()
            if choice in ("r", "retry"):
                if cover_found:
                    host = await _choose_cover_host(tracker, default_host)
                else:
                    click.secho("Looking for a cover image again...", fg="cyan")
            elif choice in ("y", "yes"):
                return True, None
            else:
                return False, None


async def _check_logs(path: str) -> None:
    """Score every rip log under the album and check its CRCs against the audio.

    Args:
        path: Album folder.

    Raises:
        click.Abort: If a log was edited, the audio could not be verified, or the user declines to
            continue after a CRC mismatch.
    """
    click.secho("\nChecking logs", fg="green")

    def _abort_on_scan_error(error: OSError) -> None:
        # os.walk would otherwise skip the folder, and its log, silently.
        click.secho(f"Could not scan {error.filename} for logs: {error}", fg="red")
        raise click.Abort() from error

    for root, _, files in os.walk(path, onerror=_abort_on_scan_error):
        for f in files:
            if not f.lower().endswith(".log"):
                continue
            filepath = os.path.join(root, f)
            click.secho(f"\nScoring {filepath}...", fg="cyan", bold=True)
            try:
                await check_log_cambia(filepath, path)
            except EditedLogError as e:
                raise click.Abort() from e
            except CRCMismatchError as e:
                click.secho("Error: CRC mismatch between log and audio files!", fg="red", bold=True)
                if not await interaction.confirm(
                    click.style(
                        "Log file CRC does not match audio files. Do you want to continue upload anyway?",
                        fg="magenta",
                    ),
                    default=False,
                ):
                    raise click.Abort() from e
            except LogCheckSkipped as e:
                click.secho(f"Log not checked: {e}", fg="yellow")
            except Exception as e:
                # Any other failure is one while verifying the audio, which must not pass as verified.
                click.secho(f"Could not verify the audio against {filepath}: {e}", fg="red")
                raise click.Abort() from e


def _do_not_upload_refusal(tracker: str, release: dict[str, Any], said: str | None = None) -> str | None:
    """Why the tracker's Do-Not-Upload list forbids the release, if it does, saying it unless it was said already.

    Nothing skips it, -yyy included.

    Args:
        release: The release's rls_data or metadata.
        said: The reason a check of the same tracker gave before the review, if any.
    """
    reason = do_not_upload_reason(tracker, Candidate.from_metadata(release))
    if reason is not None and reason != said:
        click.secho(f"\nNot uploading to {tracker}: {reason}", fg="red", bold=True)
    elif reason is None and said is not None:
        click.secho(f"\nAs reviewed, the release is not on {tracker}'s Do-Not-Upload list.", fg="yellow")
    return reason


def _sixteen_bit_refusal(tracker: str, audio_info: dict[str, Any]) -> bool:
    """Say what the tracker's rule on 16bit files above 48 kHz means for the files, and whether it refuses them.

    A tracker that only trumps them gets a warning and the upload goes on. Nothing skips a refusal, -yyy included.
    """
    site_class = salmon.trackers.tracker_classes.get(tracker)
    rule = site_class.TAG_RULES.sixteen_bit_above_48khz if site_class else ""
    notice = sixteen_bit_notice(tracker, rule, audio_info)
    if notice is None:
        return False
    if rule == "refused":
        click.secho(f"\nNot uploading to {tracker}: {notice}", fg="red", bold=True)
        return True
    click.secho(f"\n{notice}", fg="yellow")
    return False


def _warn_about_provenance(path: str) -> None:
    """Print each ripper or store marker in the tags that the audio contradicts.

    Read before the files are retagged, which can blank or replace their comments. Only warns: it never
    stops the upload or changes an answer, and prints nothing when no marker contradicts the audio.
    """
    contradictions = gather_provenance(path)["contradictions"]
    if contradictions:
        click.secho("\nTag markers the audio contradicts:", fg="yellow", bold=True)
        for note in contradictions:
            click.secho(f"  - {note}", fg="yellow")


async def upload(
    gazelle_site: "BaseGazelleApi",
    path: str,
    group_id: int | None,
    source: str | None,
    lossy: bool | None,
    spectrals: tuple[int, ...],
    encoding: str | None,
    scene: bool = False,
    overwrite_meta: bool = False,
    recompress: bool = False,
    source_url: str | None = None,
    searchstrs: list[str] | None = None,
    request_id: int | str | None = None,
    spectrals_after: bool = False,
    auto_rename: bool = False,
    skip_up: bool = False,
    skip_mqa: bool = False,
    skip_log_check: bool = False,
    skip_integrity_check: bool = False,
    essential_only: bool = False,
    flac_group: dict[str, Any] | None = None,
    skip_initial_review: bool = False,
    apply_ai_suggestions: bool = False,
    trackers: list[str] | None = None,
) -> None:
    """Upload an album folder to Gazelle Site.

    Offer the choice to upload to another tracker after completion, unless `trackers` names them.

    Args:
        gazelle_site: The tracker API instance.
        path: Path to the album folder.
        group_id: Optional existing group ID.
        source: Media source (CD, WEB, etc).
        lossy: Whether files are lossy mastered.
        spectrals: Track numbers for spectrals.
        encoding: Audio encoding.
        scene: Whether this is a scene release.
        overwrite_meta: Whether to overwrite metadata.
        recompress: Whether to recompress FLACs.
        source_url: Source URL for WEB uploads.
        searchstrs: Search strings for dupe checking.
        request_id: Request ID to fill.
        spectrals_after: Check spectrals after upload.
        auto_rename: Auto-rename files and folders.
        skip_up: Skip upconvert check.
        skip_mqa: Skip MQA check.
        skip_log_check: Skip log checking.
        skip_integrity_check: Skip integrity check.
        essential_only: If True, only essential extensions are allowed.
        flac_group: The existing group that already holds this release's FLAC, as the tracker's
            torrentgroup API returns it. If given, the FLAC is not uploaded: only transcodes of it are,
            into that group.
        skip_initial_review: Skip the first manual metadata review before AI review.
        apply_ai_suggestions: Automatically apply AI review suggestions when present.
        trackers: The site codes to upload to, in order, gazelle_site's first: the run uploads to each and
            asks about no other. None asks after each upload, as configured.
    """
    path = os.path.abspath(path)
    # Read before staging and renaming: the record knows the folder by the name the converter gave it.
    conversion = conversion_of(path)
    if flac_group is not None and (refusal := validate_skip_flac_source(path)):
        return click.secho(f"\n{refusal}", fg="red", bold=True)
    # The group's FLAC is most likely seeding from path, so with --skip-flac-upload everything works on a copy.
    # So does a dry run, which changes nothing, and an album in library_dirs: see staged_source.
    with (
        staged_source(path, scratch=flac_group is not None or dryrun.active()) as (staged, rename_into),
        # A scratch copy's run directory, removed when the run ends: where a dry run writes.
        dryrun.writing_into(rename_into),
    ):
        await _upload_staged(
            gazelle_site,
            staged,
            group_id,
            source,
            lossy,
            spectrals,
            encoding,
            scene=scene,
            overwrite_meta=overwrite_meta,
            recompress=recompress,
            source_url=source_url,
            searchstrs=searchstrs,
            request_id=request_id,
            spectrals_after=spectrals_after,
            auto_rename=auto_rename,
            skip_up=skip_up,
            skip_mqa=skip_mqa,
            skip_log_check=skip_log_check,
            skip_integrity_check=skip_integrity_check,
            essential_only=essential_only,
            flac_group=flac_group,
            skip_initial_review=skip_initial_review,
            apply_ai_suggestions=apply_ai_suggestions,
            rename_into=rename_into,
            library_album=path if cfg.directory.is_library_path(path) else None,
            conversion=conversion,
            trackers=trackers,
        )


def converted_from_note(conversion: dict[str, Any] | None, url: str | None) -> str | None:
    """The conversion note for a folder a converter made, worded as the in-run conversion upload's."""
    if not conversion:
        return None
    click.secho(f"\nThis folder was converted from {conversion.get('source')}; describing the conversion.", fg="cyan")
    if conversion.get("kind") == "transcode":
        return transcode_note(url or "", cast("Bitrate", conversion.get("bitrate")))
    return conversion_note(url or "", conversion.get("sample_rate"), cast("BitDepth", conversion.get("bit_depth", 16)))


def _trackers_for_run(
    first_tracker: str, flac_group: dict[str, Any] | None, trackers: list[str] | None = None
) -> list[str]:
    """Get the site codes of every tracker an upload run may upload to, first_tracker first.

    With --skip-flac-upload, or without multi_tracker_upload, the run stops after the first tracker.
    Trackers named with -t are the run's, whatever multi_tracker_upload says.
    """
    if flac_group is not None:
        return [first_tracker]
    if trackers:
        return list(trackers)
    if not cfg.upload.multi_tracker_upload:
        return [first_tracker]
    return [first_tracker, *(code for code in salmon.trackers.tracker_list if code != first_tracker)]


def _max_path_length_for_run(gazelle_site: "BaseGazelleApi", trackers: list[str] | None = None) -> int:
    """The path limit to check the folder against: this tracker's, or the strictest among every
    tracker configured, when the run might go on to upload to another of them afterward.

    Which trackers a run actually reaches is only known one at a time: after each upload, the
    user is offered a choice among the remaining configured trackers. The folder is checked once,
    before the first upload, so it is checked against the strictest limit already knowable then.
    Trackers named with -t are known up front: the strictest of theirs.
    """
    own_limit = getattr(gazelle_site, "TAG_RULES", TagRules()).max_path_length
    if not trackers and not cfg.upload.multi_tracker_upload:
        return own_limit
    limits = [own_limit]
    for code in trackers or salmon.trackers.tracker_list:
        tracker_class = salmon.trackers.tracker_classes.get(code)
        if tracker_class is not None:
            limits.append(tracker_class.TAG_RULES.max_path_length)
    return min(limits)


async def _upload_staged(
    gazelle_site: "BaseGazelleApi",
    path: str,
    group_id: int | None,
    source: str | None,
    lossy: bool | None,
    spectrals: tuple[int, ...],
    encoding: str | None,
    *,
    scene: bool,
    overwrite_meta: bool,
    recompress: bool,
    source_url: str | None,
    searchstrs: list[str] | None,
    request_id: int | str | None,
    spectrals_after: bool,
    auto_rename: bool,
    skip_up: bool,
    skip_mqa: bool,
    skip_log_check: bool,
    skip_integrity_check: bool,
    essential_only: bool,
    flac_group: dict[str, Any] | None,
    skip_initial_review: bool,
    apply_ai_suggestions: bool,
    rename_into: str | None,
    library_album: str | None,
    conversion: dict[str, Any] | None = None,
    trackers: list[str] | None = None,
) -> None:
    """Run upload() on a folder that is safe to change; see upload() for the arguments.

    Args:
        conversion: What the folder was converted from, when a converter made it: the upload describes it.
        rename_into: The directory the renamed folder goes into, instead of download_directory.
        library_album: The folder path is a copy of, when that folder must be kept: "delete" deletes only the copy.
    """
    remove_downloaded_cover_image = scene or cfg.image.remove_auto_downloaded_cover_image
    if not source:
        source = await _prompt_source(detect_source(path))
    audio_info = gather_audio_info(path)
    hybrid = check_hybrid(audio_info)
    if not scene:
        standardize_tags(path)
    tags = gather_tags(path)
    rls_data = await construct_rls_data(
        tags,
        audio_info,
        source,
        encoding,
        scene=scene,
        overwrite=overwrite_meta,
        prompt_encoding=True,
        hybrid=hybrid,
    )

    if flac_group is not None and (refusal := validate_skip_flac_source(path, rls_data)):
        return click.secho(f"\n{refusal}", fg="red", bold=True)
    source_flac = None

    dupe_searchstrs: list[str] = []
    # A release the first tracker's list forbids gets no group search there, so no prompt to pick a group. The
    # review may change the names: the list is checked again after it.
    tags_refusal = _do_not_upload_refusal(gazelle_site.site_code, rls_data)
    # The files never change in the review. With --skip-flac-upload only transcodes go up: no FLAC to refuse.
    rate_refused = flac_group is None and _sixteen_bit_refusal(gazelle_site.site_code, audio_info)
    if group_id is None:
        # Left empty for a listed release: if the review takes it off the list, recheck_dupe then searches.
        searchstrs = dupe_searchstrs = (
            []
            if tags_refusal or rate_refused
            else generate_dupe_check_searchstrs(rls_data["artists"], rls_data["title"], rls_data["catno"])
        )

    try:
        # The search for an existing group only reads from the tracker, so it runs in the background during
        # the MQA, upconvert and log checks below, and what it found is shown once they are done.
        async with fetch_existing_group_candidates_in_background(
            gazelle_site, dupe_searchstrs, rls_data["title"]
        ) as group_fetch:
            if not skip_mqa:
                all_files = cfg.upload.mqa_check_all_tracks
                checked = "every FLAC file" if all_files else "first FLAC file only"
                click.secho(f"Checking for MQA release ({checked})", fg="cyan", bold=True)
                await mqa_test(path, all_files=all_files)
                click.secho("No MQA release detected", fg="green")

            if rls_data["encoding"] == "24bit Lossless" and not skip_up:
                if not await interaction.assume_defaults():
                    if await interaction.confirm(
                        click.style(
                            "\n24bit detected. Do you want to check whether might be upconverted?", fg="magenta"
                        ),
                        default=True,
                    ):
                        await upload_upconvert_test(path)
                else:
                    await upload_upconvert_test(path)

            if source == "CD" and not skip_log_check:
                await _check_logs(path)

            _warn_about_provenance(path)

            if group_fetch is not None:
                results, recent_uploads = await group_fetch.result()
                group_id = await resolve_existing_group(
                    gazelle_site, dupe_searchstrs, results, recent_uploads, release=rls_data
                )

            spectral_ids = None
            lossy_master: bool = False
            if spectrals_after:
                # We tell the uploader not to worry about it being lossy until later.
                pass
            else:
                lossy_result, spectral_ids = await check_spectrals(
                    path,
                    audio_info,
                    lossy,
                    spectrals,
                    format=rls_data["format"],
                    hosts=specs_hosts_text(_trackers_for_run(gazelle_site.site_code, flac_group, trackers)),
                )
                lossy_master = lossy_result if lossy_result is not None else False

            metadata, new_source_url = await get_metadata(path, tags, rls_data)
            if new_source_url is not None:
                source_url = new_source_url
                click.secho(f"New Source URL: {source_url}", fg="yellow")
            path, metadata, tags, audio_info = await edit_metadata(
                path,
                tags,
                metadata,
                source_url,
                source,
                rls_data,
                recompress,
                auto_rename,
                spectral_ids,
                skip_integrity_check,
                essential_only,
                skip_initial_review,
                apply_ai_suggestions,
                rename_into=rename_into,
                max_path_length=_max_path_length_for_run(gazelle_site, trackers),
                rls_type_hint=suggest_release_type(
                    rls_data.get("title"), [info.get("duration") or 0 for info in audio_info.values()]
                ),
            )

            # Before anything is made or sent for the first tracker: its group, spectrals, cover and upload.
            first_listed = (
                rate_refused or _do_not_upload_refusal(gazelle_site.site_code, metadata, said=tags_refusal) is not None
            )
            if not group_id and not first_listed:
                group_id = await recheck_dupe(gazelle_site, searchstrs, metadata)
                click.echo()
            # From here on, the review may have changed the artists, title or catno, so search strings and
            # our title come from the reviewed metadata, not the pre-review rls_data.
            searchstrs = generate_dupe_check_searchstrs(metadata["artists"], metadata["title"], metadata["catno"])
            our_title = metadata["title"]
            track_data = concat_track_data(tags, audio_info)
            if flac_group is not None:
                # Matched on the reviewed metadata, so an edited catalogue number or edition title moves the pick.
                source_flac = await choose_source_flac(flac_group, metadata)
                if source_flac is None:
                    raise click.Abort
            if not scene:
                # Before any torrent or transcode is made from the folder, so they all get the same files.
                check_embedded_pictures(path)
    except click.Abort:
        return click.secho("\nAborting upload...", fg="red")
    except AbortAndDeleteFolder:
        if dryrun.active():
            dryrun.say("not deleting the music folder.")
            return click.secho("\nAborting upload...", fg="red")
        if flac_group is not None:
            click.secho(
                "\nNot deleting the music folder: with --skip-flac-upload the source is never modified.",
                fg="yellow",
                bold=True,
            )
            return click.secho("\nAborting upload...", fg="red")
        if cfg.directory.protects(path):
            click.secho(f"\nNot deleting {path}: it is in library_dirs, or holds one.", fg="yellow", bold=True)
            return click.secho("\nAborting upload...", fg="red")
        if library_album is not None:
            click.secho(
                f"\nDeleting the copy the upload worked on. The library album {library_album} is kept.",
                fg="yellow",
                bold=True,
            )
        if platform.system() == "Windows" and cfg.upload.windows_use_recycle_bin:
            try:
                import send2trash

                send2trash.send2trash(path)
                return click.secho("\nMoved folder to recycle bin, aborting upload...", fg="red")
            except Exception as e:
                click.secho(f"\nError moving folder to recycle bin: {e}", fg="red")
                return click.secho("\nAborting upload...", fg="red")
        else:
            shutil.rmtree(path)
            return click.secho("\nDeleted folder, aborting upload...", fg="red")

    # Uploaded once per specs host; the files are deleted once no tracker of the run can need them.
    spectral_uploads = SpectralUploads(path, _trackers_for_run(gazelle_site.site_code, flac_group, trackers))
    try:
        lossy_comment = None
        spectral_urls = None
        if not spectrals_after:
            if lossy_master:
                lossy_comment = await generate_lossy_approval_comment(source_url, list(track_data.keys()))
                click.echo()

            if not first_listed:
                spectral_urls = await spectral_uploads.urls_for(gazelle_site.site_code, spectral_ids)
        if cfg.upload.requests.last_minute_dupe_check and not first_listed:
            await last_min_dupe_check(gazelle_site, searchstrs, our_title)

        # Shallow copy to avoid errors on multiple uploads in one session. Trackers named with -t are
        # uploaded to in their order, without asking.
        remaining_gazelle_sites = list(trackers or salmon.trackers.tracker_list)
        # Whether the run goes on after a tracker: always for named ones, until none is left.
        go_on = bool(trackers) or cfg.upload.multi_tracker_upload
        tracker = gazelle_site.site_code
        if first_listed:
            remaining_gazelle_sites.remove(tracker)
            if request_id is not None:
                # --request names a request of this tracker: another tracker's request with that ID is another one.
                click.secho(f"\nNot filling request {request_id}: it is {tracker}'s.", fg="yellow")
                request_id = None
            tracker = None
        torrent_id = None
        cover_url = None
        cover_urls: dict[str, str | None] = {}  # Uploaded cover URL per image host, reused across trackers

        seedbox_uploader = UploadManager()
        uploaded: list[str] = []  # The URL of each torrent uploaded, for an abort to list
        flac_url = f"{gazelle_site.base_url}/torrents.php?torrentid={source_flac['id']}" if source_flac else None

        try:
            while True:
                # Loop until we don't want to upload to any more sites.
                if not tracker:
                    # After a tracker whose Do-Not-Upload list forbids the release: the run may have none left.
                    if flac_url or not remaining_gazelle_sites or not go_on:
                        break
                    if trackers:
                        tracker = remaining_gazelle_sites[0]
                        click.secho(f"\nNext tracker: {tracker}", fg="magenta")
                    else:
                        click.secho("\nWould you like to upload to another tracker? ", fg="magenta", nl=False)
                        tracker = await salmon.trackers.choose_tracker(remaining_gazelle_sites)
                    if not tracker:
                        click.secho("\nDone with this release.", fg="green")
                        break
                    gazelle_site = salmon.trackers.get_class(tracker)()
                    # Before its dupe check, which may ask which group to upload into.
                    if _do_not_upload_refusal(tracker, metadata) is not None or (
                        flac_group is None and _sixteen_bit_refusal(tracker, audio_info)
                    ):
                        remaining_gazelle_sites.remove(tracker)
                        tracker = None
                        continue

                    click.secho(f"Uploading to {gazelle_site.base_url}", fg="cyan", bold=True)
                    # A torrent already seeds from the folder: never offer to delete it.
                    # Matched on the reviewed metadata: the review may have changed the artists, title or year.
                    try:
                        group_id = await check_existing_group(
                            gazelle_site, searchstrs, offer_deletion=False, our_title=our_title, release=metadata
                        )
                    except RequestError as e:
                        # Like a failed upload: skip this tracker, and offer the next one.
                        click.secho(f"\nUpload to {gazelle_site.site_string} failed: {e}", fg="red", bold=True)
                        remaining_gazelle_sites.remove(tracker)
                        tracker = None
                        if not remaining_gazelle_sites or not go_on:
                            break
                        continue

                remaining_gazelle_sites.remove(tracker)

                # Handle cover image for this tracker
                proceed, cover_url = await resolve_cover_url(
                    gazelle_site, group_id, cover_urls, path, metadata["cover"], remove_downloaded_cover_image
                )
                if not proceed:
                    # Like a failed upload: skip this tracker, and offer the next one.
                    click.secho(f"\nSkipping upload to {gazelle_site.site_string}.", fg="red", bold=True)
                    tracker = None
                    if not remaining_gazelle_sites or not go_on:
                        break
                    continue

                if not spectrals_after:
                    # Uploaded to this tracker's specs host, unless a tracker using the same host already did.
                    spectral_urls = await spectral_uploads.urls_for(gazelle_site.site_code, spectral_ids)

                if not scene and cfg.image.auto_compress_cover:
                    compress_pictures(path)

                if not flac_url and not request_id and cfg.upload.requests.check_requests:
                    request_id = await check_requests(gazelle_site, searchstrs)

                try:
                    held: set[str] = set()
                    if flac_url and source_flac is not None:
                        click.secho(f"\nNot uploading the FLAC: transcoding from {flac_url}", fg="yellow")
                        url = flac_url
                        formats = {
                            option["name"]: downconversion_format(option)
                            for option in get_downconversion_options(rls_data, track_data, quiet=True)
                        }
                        held = held_formats(flac_group or {}, metadata, source_flac, formats)
                    else:
                        group_link = f"{gazelle_site.base_url}/torrents.php?id={group_id}" if group_id else source_url
                        torrent_id, group_id, torrent_path, torrent_content, url = await upload_and_report(
                            gazelle_site,
                            path,
                            group_id,
                            metadata,
                            cover_url,
                            track_data,
                            hybrid,
                            lossy_master,
                            spectral_urls,
                            spectral_ids,
                            lossy_comment,
                            request_id,
                            source_url,
                            seedbox_uploader,
                            source=source,
                            conversion_note=converted_from_note(conversion, group_link),
                        )

                        request_id = None
                        uploaded.append(url)

                        if not dryrun.active():
                            await print_torrents(gazelle_site, group_id, highlight_torrent_id=torrent_id)

                        if spectrals_after:
                            # Once, on the first torrent up, whether or not the run goes on to another tracker.
                            # Its transcodes, and the later trackers' uploads, then carry what it found.
                            spectrals_after = False
                            lossy_master, lossy_comment, spectral_urls, spectral_ids = await post_upload_spectral_check(
                                gazelle_site,
                                path,
                                torrent_id,
                                None,
                                track_data,
                                source,
                                source_url,
                                format=rls_data["format"],
                                uploads=spectral_uploads,
                            )

                    # Nothing to convert (an MP3 upload) asks nothing.
                    if get_downconversion_options(rls_data, track_data, quiet=True) and (
                        flac_url
                        or await interaction.assume_defaults()
                        or await interaction.confirm(
                            click.style("\nWould you like to check downconversion options?", fg="magenta"),
                            default=True,
                        )
                    ):
                        selected_tasks = await prompt_downconversion_choice(rls_data, track_data, held)
                        if selected_tasks:
                            display_names = [task["name"] for task in selected_tasks]
                            click.secho(
                                f"\nSelected formats for downconversion: {', '.join(display_names)}",
                                fg="green",
                                bold=True,
                            )

                            # Execute downconversion tasks
                            await execute_downconversion_tasks(
                                selected_tasks,
                                path,
                                gazelle_site,
                                group_id,
                                metadata,
                                cover_url,
                                track_data,
                                hybrid,
                                lossy_master,
                                spectral_urls,
                                spectral_ids,
                                lossy_comment,
                                request_id,
                                source_url,
                                seedbox_uploader,
                                source,
                                url,
                                uploaded=uploaded,
                            )
                except RequestError as e:
                    click.secho(f"\nUpload to {gazelle_site.site_string} failed: {e}", fg="red", bold=True)
                    if isinstance(e, UnknownOutcomeError):
                        record.note_unknown_outcome(e)

                tracker = None
                if flac_url or not remaining_gazelle_sites or not go_on:
                    click.secho("\nDone uploading this release.", fg="green")
                    break

        except click.Abort:
            if not uploaded or dryrun.active():
                raise
            # What is up stays up and is seeded below: the run only stops offering more.
            click.secho("\nAborting: nothing more is uploaded. Already uploaded:", fg="red")
            for line in uploaded:
                click.echo(f"  {line}")
        finally:
            await seedbox_uploader.execute_upload()
    finally:
        await spectral_uploads.close()


async def edit_metadata(
    path: str,
    tags: dict[str, "TagFile"],
    metadata: dict[str, Any],
    source_url: str | None,
    source: str,
    rls_data: dict[str, Any],
    recompress: bool,
    auto_rename: bool,
    spectral_ids: dict[int, str] | None,
    skip_integrity_check: bool = False,
    essential_only: bool = False,
    skip_initial_review: bool = False,
    apply_ai_suggestions: bool = False,
    rename_into: str | None = None,
    max_path_length: int = 180,
    rls_type_hint: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, "TagFile"], dict[str, dict[str, Any]]]:
    """Edit release metadata in an interactive loop until the user confirms.

    Repeatedly prompts the user to review and edit metadata, then applies tags,
    renames files and folder, checks integrity, and confirms readiness for upload.

    Args:
        path: Path to the release directory.
        tags: Mapping of filename to TagFile objects.
        metadata: Release metadata dictionary.
        source: Source string (e.g. "WEB", "CD").
        rls_data: Release data dictionary from pre_data construction.
        recompress: Whether to recompress audio files after tagging.
        auto_rename: Whether to automatically rename files and folder.
        spectral_ids: Mapping of track index to spectral image ID, or None.
        skip_integrity_check: Whether to skip the integrity check step.
        essential_only: If True, only essential extensions are allowed.
        skip_initial_review: Skip the first manual metadata review before AI review.
        apply_ai_suggestions: Automatically apply AI review suggestions when present.
        rename_into: The directory the renamed folder goes into, instead of download_directory.
        max_path_length: The longest in-torrent path the folder structure check allows.
        rls_type_hint: The release type prompt's default, when the release type has to be asked.

    Returns:
        A tuple of (path, metadata, tags, audio_info) after editing is complete.

    Raises:
        click.Abort: If a scene release fails the integrity check, or a file does not decode.
    """
    while True:
        metadata = await review_metadata_with_ai(
            metadata,
            rls_data,
            source_url,
            metadata_validator,
            functools.partial(review_metadata, rls_type_hint=rls_type_hint),
            skip_initial_review=skip_initial_review,
            apply_suggestions=apply_ai_suggestions,
        )
        if not metadata["scene"]:
            await tag_files(path, tags, metadata, auto_rename)

        tags = await check_tags(path)
        tag_messages = process_tag_issues(
            path,
            gather_audio_info(path),
            scene=metadata["scene"],
            recompress=recompress and not metadata["scene"],
        )
        if tag_messages:
            click.secho("\nTag notes:", fg="yellow", bold=True)
            for message in tag_messages:
                click.secho(f"  - {message}", fg="yellow")
        if not metadata["scene"] and recompress:
            try:
                await recompress_path(path)
            except UploadError as e:
                raise UploadError(f"{e} Rerun without -c.") from e
        path = await rename_folder(path, metadata, auto_rename, parent=rename_into)
        if not metadata["scene"]:
            await rename_files(path, tags, metadata, auto_rename, spectral_ids, source)
        await check_folder_structure(
            path, metadata["scene"], essential_only=essential_only, max_path_length=max_path_length
        )

        if not skip_integrity_check:
            await resolve_integrity_for_upload(
                path, scene=metadata["scene"], assume_yes=await interaction.assume_defaults()
            )

        if await interaction.assume_defaults() or await interaction.confirm(
            click.style("\nWould you like to upload the torrent? (No to re-run metadata section)", fg="magenta"),
            default=True,
        ):
            metadata["tags"] = convert_genres(metadata["genres"])
            break

        # Refresh tags to accomodate differences in file structure.
        tags = gather_tags(path)

    tags = gather_tags(path)
    audio_info = gather_audio_info(path)
    return path, metadata, tags, audio_info


async def recheck_dupe(gazelle_site, searchstrs, metadata):
    """Rechecks for a dupe if the artist, album or catno have changed.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Original search strings.
        metadata: Release metadata.

    Returns:
        Group ID if found, None otherwise.
    """
    new_searchstrs = generate_dupe_check_searchstrs(metadata["artists"], metadata["title"], metadata["catno"])
    if searchstrs and any(n not in searchstrs for n in new_searchstrs) or not searchstrs and new_searchstrs:
        click.secho(
            f"\nRechecking for dupes on {gazelle_site.site_string} due to metadata changes...",
            fg="cyan",
            bold=True,
            nl=False,
        )
        return await check_existing_group(gazelle_site, new_searchstrs, our_title=metadata["title"], release=metadata)
    return None


async def last_min_dupe_check(gazelle_site, searchstrs, our_title=None):
    """Check for dupes in the log one last time before upload.

    Helpful if you are uploading something in race like conditions.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings for dupe checking.
        our_title: Our release's title, passed through to dupe_check_recent_torrents.
    """
    if not can_check_site_log(gazelle_site):
        return
    # Should really avoid asking if already shown the same releases from the log.
    click.secho(f"Last Minute Dupe Check on {gazelle_site.site_code}", fg="cyan")
    recent_uploads = await dupe_check_recent_torrents(gazelle_site, searchstrs, our_title)
    if recent_uploads:
        print_recent_upload_results(gazelle_site, recent_uploads, " / ".join(searchstrs))
        if not await interaction.confirm(
            click.style(
                "\nWould you still like to upload?",
                fg="red",
                bold=True,
            ),
            default=False,
        ):
            raise click.Abort
    else:
        click.secho(f"Nothing found on {gazelle_site.site_code}", fg="green")


def metadata_validator(metadata):
    """Validate that the provided metadata is not an issue."""
    metadata = metadata_validator_base(metadata)
    if metadata["format"] not in FORMATS.values():
        raise InvalidMetadataError(f"{metadata['format']} is not a valid format.")
    if metadata["encoding"] not in ENCODINGS:
        raise InvalidMetadataError(f"{metadata['encoding']} is not a valid encoding.")

    return metadata


def get_downconversion_options(rls_data, track_data, quiet: bool = False):
    """
    Determine available downconversion options based on current format.
    Returns a list of downconversion tasks

    Tier hierarchy:
    1. 24bit 176.4 ~ 192 kHz
    2. 24bit 44.1 ~ 96 kHz
    3. 16bit 44.1 ~ 48 kHz
    4. mp3 320
    5. mp3 v0

    A release whose files differ in sample rate gets only the MP3 transcodes: a lossless
    downconversion has one target rate, and would upsample the files below it.
    `quiet` leaves out the message saying so, for a caller that only asks whether there are options.
    """
    if not track_data:
        return []

    rates = sorted({track["sample rate"] for track in track_data.values()})
    sample_rate = rates[0]
    encoding = rls_data["encoding"]
    mixed = len(rates) > 1
    if mixed and not quiet and encoding == "24bit Lossless":
        listed = ", ".join(f"{rate / 1000:g}" for rate in rates)
        click.secho(f"No lossless downconversion: the files have different sample rates ({listed} kHz).", fg="yellow")

    options = []

    # Tier 1: 24bit 176.4~192 kHz
    if encoding == "24bit Lossless" and not mixed and sample_rate >= 176400:
        # Can downconvert to 24bit lower sample rate
        target_rate = 96000 if sample_rate % 48000 == 0 else 88200
        options.append(
            {
                "name": f"24bit {target_rate / 1000:.1f} kHz",
                "action": "downconvert",
                "target_bitdepth": 24,
                "target_sample_rate": target_rate,
            }
        )

    # Tier 2: 24bit 44.1~96 kHz
    if encoding == "24bit Lossless" and not mixed and sample_rate >= 44100:
        # Can downconvert to 16bit
        target_rate = 48000 if sample_rate % 48000 == 0 else 44100
        options.append(
            {
                "name": f"16bit {target_rate / 1000:.1f} kHz",
                "action": "downconvert",
                "target_bitdepth": 16,
                "target_sample_rate": target_rate,
            }
        )

    # Tier 3: 16bit 44.1~48 kHz
    if (encoding == "Lossless") or (encoding == "24bit Lossless"):
        # Can transcode to MP3
        options.extend(
            [
                {"name": "MP3 320", "action": "transcode", "encoding": "320"},
                {"name": "MP3 V0", "action": "transcode", "encoding": "V0"},
            ]
        )

    return options


def downconversion_format(task: dict[str, Any]) -> tuple[str, str]:
    """Give the format and encoding of the torrent a downconversion task makes."""
    if task["action"] == "transcode":
        return "MP3", {"320": "320", "V0": "V0 (VBR)"}[task["encoding"]]
    return "FLAC", "Lossless" if task["target_bitdepth"] == 16 else "24bit Lossless"


async def prompt_downconversion_choice(rls_data, track_data, held: set[str] | frozenset[str] = frozenset()):
    """
    Prompt user to select downconversion formats.
    Returns a list of selected task dictionaries.
    Options named in `held` are already in the edition: they are flagged as a dupe risk and left out
    of the default choice and of --yes-all, but can still be picked by number.
    """
    options = get_downconversion_options(rls_data, track_data)

    if not options:
        return []

    for name in sorted(held):
        click.secho(
            f"\nDUPE RISK: this edition already has {name}; the site removes exact duplicates.", fg="red", bold=True
        )
    unheld = [option for option in options if option["name"] not in held]
    if await interaction.assume_defaults():
        return unheld

    click.secho("\nDownconversion Options", fg="cyan", bold=True)

    # Get current format info for display
    encoding = rls_data["encoding"]
    if track_data:
        sample_rate = next(iter(track_data.values()))["sample rate"]
        current_format = f"{encoding}"
        if encoding == "24bit Lossless" or encoding == "Lossless":
            current_format += f" ({sample_rate / 1000:.1f} kHz)"
    else:
        current_format = encoding

    click.secho(f"Current format: {current_format}", fg="yellow")
    click.secho("Available downconversion formats:", fg="green")

    for i, option in enumerate(options, 1):
        click.secho(f"  {i}. {option['name']}", fg="white")

    click.secho("  0. Skip downconversion", fg="white")
    click.secho("  *. All formats", fg="white")
    if len(unheld) == len(options):
        default = "*"
    else:
        default = " ".join(str(i) for i, option in enumerate(options, 1) if option in unheld) or "0"

    selected_tasks = []

    while True:
        try:
            choices = await interaction.prompt(
                click.style(
                    '\nSelect formats to convert (space-separated list of IDs, "0" for none, "*" for all)', fg="magenta"
                ),
                default=default,
            )

            if choices.strip() == "0":
                break

            if choices.strip() == "*":
                selected_tasks = options
                break

            # Parse choices - now using space separation
            choice_nums = [int(x.strip()) for x in choices.split() if x.strip().isdigit()]

            # Validate choices
            invalid_choices = [x for x in choice_nums if x < 1 or x > len(options)]
            if invalid_choices:
                click.secho(
                    f"Invalid choices: {invalid_choices}. Please enter numbers between 1-{len(options)}.", fg="red"
                )
                continue

            # Get selected tasks
            selected_tasks = [options[i - 1] for i in choice_nums]

            # Confirm selection
            if selected_tasks:
                display_names = [task["name"] for task in selected_tasks]
                click.secho(f"\nSelected formats: {', '.join(display_names)}", fg="green")
                if await interaction.confirm(click.style("Confirm selection?", fg="magenta"), default=True):
                    break
            else:
                break

        except (ValueError, IndexError):
            click.secho("Invalid input format, please enter numeric options", fg="red")
            continue

    return selected_tasks


async def execute_downconversion_tasks(
    selected_tasks: list[dict[str, Any]],
    path: str,
    gazelle_site: "BaseGazelleApi",
    group_id: int | None,
    metadata: dict[str, Any],
    cover_url: str | None,
    track_data: dict[str, Any],
    hybrid: bool,
    lossy_master: bool,
    spectral_urls: dict[int, list[str]] | None,
    spectral_ids: dict[int, str] | None,
    lossy_comment: str | None,
    request_id: int | str | None,
    source_url: str | None,
    seedbox_uploader: UploadManager,
    source: str | None,
    base_url: str,
    *,
    uploaded: list[str] | None = None,
) -> None:
    """Execute the selected downconversion tasks.

    Args:
        selected_tasks: List of downconversion task dicts.
        path: Path to the album folder.
        gazelle_site: The tracker API instance.
        group_id: Optional existing group ID.
        metadata: Release metadata.
        cover_url: Cover image URL.
        track_data: Track information.
        hybrid: Whether this is a hybrid release.
        lossy_master: Whether this is lossy mastered.
        spectral_urls: Spectral image URLs.
        spectral_ids: Spectral IDs.
        lossy_comment: Lossy approval comment.
        request_id: Request ID to fill.
        source_url: Source URL.
        seedbox_uploader: Seedbox upload manager.
        source: Media source.
        base_url: Base URL for the original upload.
        uploaded: Where to add the URL of each torrent uploaded.
    """
    if uploaded is None:
        uploaded = []

    base_path = path
    # A dry run's go into its scratch directory, removed with it.
    output_dir = dryrun.scratch_dir() if dryrun.active() else cfg.directory.download_directory

    override_lossy_comment = (
        f"Transcode of {base_url}\n[hide=Lossy comment of original torrent]{lossy_comment}[/hide]\n"
        if lossy_comment
        else None
    )

    for task in selected_tasks:
        click.secho(f"\nProcessing: {task['name']}", fg="cyan", bold=True)

        if task["action"] == "downconvert":
            # Execute downconversion
            sample_rate, new_path = await convert_folder(
                base_path,
                bit_depth=task["target_bitdepth"],
                sample_rate=task["target_sample_rate"],
                output_dir=output_dir,
            )
            await anyio.sleep(0.1)

            # The upload describes the converted files (their sample rate, for one), not the source's.
            # A folder that was already there may hold other files: then it is not this conversion.
            try:
                converted_info = gather_audio_info(new_path)
            except (UploadError, MutagenError) as e:
                click.secho(f"  Could not read {new_path} ({e}): not uploading it.", fg="red", bold=True)
                continue
            if converted_info.keys() != track_data.keys():
                click.secho(
                    f"  {new_path} does not hold the same audio files as the source: not uploading it.",
                    fg="red",
                    bold=True,
                )
                continue
            # Nor is it this conversion if its files are not in the format the task makes.
            expected = (task["target_bitdepth"], task["target_sample_rate"])
            found = sorted({(info["precision"], info["sample rate"]) for info in converted_info.values()})
            if found != [expected]:
                found_formats = ", ".join(f"{bits} bit {rate / 1000:g} kHz" for bits, rate in found)
                click.secho(
                    f"  {new_path} holds {found_formats} files, not {expected[0]} bit {expected[1] / 1000:g} kHz: "
                    "not uploading it.",
                    fg="red",
                    bold=True,
                )
                continue
            conversion_track_data = {name: {**track, **converted_info[name]} for name, track in track_data.items()}

            # Update metadata for this conversion
            conversion_metadata = metadata.copy()
            conversion_metadata["format"], conversion_metadata["encoding"] = downconversion_format(task)

            # Generate description for conversion
            description = generate_conversion_description(base_url, sample_rate, task["target_bitdepth"])
            click.secho(f"  Generated description: {description[:100]}...", fg="blue")
            await check_folder_structure(
                new_path, conversion_metadata["scene"], max_path_length=_max_path_length_for_run(gazelle_site)
            )

            # Upload the converted version
            torrent_id, group_id, torrent_path, torrent_content, new_url = await upload_and_report(
                gazelle_site,
                new_path,
                group_id,
                conversion_metadata,
                cover_url,
                conversion_track_data,
                hybrid,
                lossy_master,
                spectral_urls,
                spectral_ids,
                lossy_comment,
                request_id,
                source_url,
                seedbox_uploader,
                source=source,
                override_description=description,
                override_lossy_comment=override_lossy_comment,
            )
            uploaded.append(new_url)

            click.secho(f"  ✓ {task['name']} conversion completed", fg="green")

        elif task["action"] == "transcode":
            # Call transcode function
            click.secho(f"  Target encoding: {task['encoding']}", fg="white")

            # Execute transcoding
            transcoded_path = await transcode_folder(base_path, task["encoding"], output_dir=output_dir)
            await anyio.sleep(0.1)

            # Update metadata for this transcode
            transcode_metadata = metadata.copy()
            transcode_metadata["format"], transcode_metadata["encoding"] = downconversion_format(task)
            transcode_metadata["encoding_vbr"] = {"320": False, "V0": True}[task["encoding"]]

            # Generate description for transcode
            description = generate_transcode_description(base_url, task["encoding"])
            click.secho(f"  Generated description: {description[:100]}...", fg="blue")
            await check_folder_structure(
                transcoded_path, transcode_metadata["scene"], max_path_length=_max_path_length_for_run(gazelle_site)
            )

            # Upload the transcoded version
            torrent_id, group_id, torrent_path, torrent_content, new_url = await upload_and_report(
                gazelle_site,
                transcoded_path,
                group_id,
                transcode_metadata,
                cover_url,
                track_data,
                hybrid,
                lossy_master,
                spectral_urls,
                spectral_ids,
                lossy_comment,
                request_id,
                source_url,
                seedbox_uploader,
                source=source,
                override_description=description,
                override_lossy_comment=override_lossy_comment,
            )
            uploaded.append(new_url)

            click.secho(f"  ✓ {task['name']} transcode completed", fg="green")


async def upload_and_report(
    gazelle_site: "BaseGazelleApi",
    path: str,
    group_id: int | None,
    metadata: dict[str, Any],
    cover_url: str | None,
    track_data: dict[str, Any],
    hybrid: bool,
    lossy_master: bool,
    spectral_urls: dict[int, list[str]] | None,
    spectral_ids: dict[int, str] | None,
    lossy_comment: str | None,
    request_id: int | str | None,
    source_url: str | None,
    seedbox_uploader: UploadManager,
    source: str | None = None,
    override_description: str | None = None,
    override_lossy_comment: str | None = None,
    conversion_note: str | None = None,
) -> tuple[int, int, str, Any, str]:
    """Upload torrent and report lossy master if needed.

    Args:
        gazelle_site: The tracker API instance.
        path: Path to the album folder.
        group_id: Optional existing group ID.
        metadata: Release metadata.
        cover_url: Cover image URL.
        track_data: Track information.
        hybrid: Whether this is a hybrid release.
        lossy_master: Whether this is lossy mastered.
        spectral_urls: Spectral image URLs.
        spectral_ids: Spectral IDs.
        lossy_comment: Lossy approval comment.
        request_id: Request ID to fill.
        source_url: Source URL.
        seedbox_uploader: Seedbox upload manager.
        source: Media source.
        override_description: Override torrent description.
        override_lossy_comment: Override lossy comment.
        conversion_note: How the folder was converted, added to the generated description.

    Returns:
        Tuple of (torrent_id, group_id, torrent_path, torrent_content, url). In a dry run, what
        prepare_and_upload gives: nothing is reported, seeded or copied.
    """
    # Prepare upload parameters
    upload_kwargs = {
        "gazelle_site": gazelle_site,
        "path": path,
        "group_id": group_id,
        "metadata": metadata,
        "cover_url": cover_url,
        "track_data": track_data,
        "hybrid": hybrid,
        "lossy_master": lossy_master,
        "spectral_urls": spectral_urls,
        "spectral_ids": spectral_ids,
        "lossy_comment": lossy_comment,
        "request_id": request_id,
        "source_url": source_url,
        **({"override_description": override_description} if override_description else {}),
        **({"conversion_note": conversion_note} if conversion_note else {}),
    }

    # Execute upload
    torrent_id, group_id, torrent_path, torrent_content = await prepare_and_upload(**upload_kwargs)

    # Handle lossy master reporting
    if lossy_master:
        await report_lossy_master(
            gazelle_site,
            torrent_id,
            spectral_urls,
            spectral_ids,
            source,
            override_lossy_comment if override_lossy_comment else lossy_comment,
            source_url=source_url,
        )

    url = finish_upload(
        gazelle_site, path, torrent_id, torrent_path, torrent_content, metadata.get("format", ""), seedbox_uploader
    )
    return torrent_id, group_id, torrent_path, torrent_content, url


def finish_upload(
    gazelle_site: "BaseGazelleApi",
    path: str,
    torrent_id: int,
    torrent_path: str,
    torrent_content: Any,
    format: str,
    seedbox_uploader: UploadManager,
    *,
    copy_folder: bool = True,
) -> str:
    """Write the uploaded torrent with its URL, and queue it for the seedboxes.

    Args:
        gazelle_site: The tracker it was uploaded to.
        path: The folder the torrent was made from.
        torrent_id: The uploaded torrent's ID.
        torrent_path: Where the .torrent file is.
        torrent_content: The torrent.
        format: The torrent's format, e.g. "FLAC".
        seedbox_uploader: Seedbox upload manager.
        copy_folder: Copy the folder to the seedboxes before adding the torrent. False when they already have
            it, as for a cross-upload, whose files already seed the other tracker's torrent.

    Returns:
        The uploaded torrent's URL. In a dry run, nothing is written, seeded or copied.
    """
    url = f"{gazelle_site.base_url}/torrents.php?torrentid={torrent_id}"
    if dryrun.active():
        # Nothing was uploaded: nothing to seed, and no URL to copy.
        if cfg.upload.upload_to_seedbox:
            dryrun.say("not copying it to a seedbox or adding it to a torrent client.")
        return url

    torrent_content.comment = url
    torrent_content.write(torrent_path, overwrite=True)

    # Display success message
    click.secho(
        f"Successfully uploaded {url} ({os.path.basename(path)}).",
        fg="green",
        bold=True,
    )
    record.note_upload(gazelle_site.site_code, format, url)

    # Copy URL to clipboard
    if cfg.upload.description.copy_uploaded_url_to_clipboard:
        pyperclip.copy(url)

    # Add to seedbox upload queue
    if cfg.upload.upload_to_seedbox:
        click.secho("Add uploading task.", fg="green")
        is_flac = format.upper() == "FLAC"
        site_code = gazelle_site.site_code
        if copy_folder:
            seedbox_uploader.add_upload_task(path, task_type="folder", is_flac=is_flac, site_code=site_code)
        seedbox_uploader.add_upload_task(
            torrent_path, task_type="seed", is_flac=is_flac, folder=path, site_code=site_code
        )

    return url


def convert_genres(genres):
    """Convert the weirdly spaced genres to RED-compliant genres."""
    return ",".join(t for t in (tagify(g) for g in genres) if t)


async def _prompt_source(detected: DetectedSource | None = None) -> str:
    """Ask for the release's media source. An empty answer takes the detected one, when there is one."""
    click.echo(f"\nValid sources: {', '.join(SOURCES.values())}")
    if detected:
        click.secho(f"The files say {detected.source}: {detected.reason}.", fg="cyan")
    while True:
        sauce = await interaction.prompt(
            click.style("What is the source of this release? [a]bort", fg="magenta"),
            default=detected.source if detected else "",
        )
        try:
            return SOURCES[sauce.lower()]
        except KeyError:
            if sauce.lower().startswith("a"):
                raise click.Abort from None
            click.secho(f"{sauce} is not a valid source.", fg="red")
