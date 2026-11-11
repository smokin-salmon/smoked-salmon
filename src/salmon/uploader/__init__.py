import os
import platform
import shutil
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import asyncclick as click

import salmon.trackers
from salmon import cfg, dryrun, interaction
from salmon.checks import mqa_test
from salmon.checks.provenance import gather_provenance
from salmon.checks.source import DetectedSource, detect_source
from salmon.checks.upconverts import upload_upconvert_test
from salmon.common import commandgroup, get_audio_files
from salmon.common.files import AlbumPath
from salmon.common.strings import numeric_id
from salmon.config.validations import CONVERSION_KINDS
from salmon.constants import SOURCES, TAG_ENCODINGS
from salmon.converter.alac import convert_alac
from salmon.converter.conversions import conversion_of
from salmon.converter.downconverting import (
    BitDepth,
    conversion_note,
)
from salmon.converter.transcoding import (
    Bitrate,
    transcode_note,
)
from salmon.errors import (
    AbortAndDeleteFolder,
    DryRunRefused,
    PickOtherResults,
    RequestError,
    SkipTracker,
    UnknownOutcomeError,
)
from salmon.tagger import (
    validate_encoding,
    validate_source,
)
from salmon.tagger.audio_info import (
    check_hybrid,
    gather_audio_info,
)
from salmon.tagger.cover import (
    check_embedded_pictures,
    download_cover_if_nonexistent,
    embed_missing_cover,
    get_cover_from_path,
    has_flac_without_picture,
)
from salmon.tagger.foldername import files_only_in, remove_source_folder
from salmon.tagger.metadata import MetadataSearch, get_metadata, pick_metadata
from salmon.tagger.pre_data import construct_rls_data
from salmon.tagger.review import suggest_release_type
from salmon.tagger.sources import without_tracking
from salmon.tagger.tags import gather_tags, standardize_tags
from salmon.trackers.connection import print_certificate_hint
from salmon.uploader import record
from salmon.uploader.conversion_choice import (
    _applicable_conversions,
    _held_conversions,
    choose_conversions,
    get_downconversion_options,
    validate_conversions,
)
from salmon.uploader.conversion_upload import _max_path_length_for_run, execute_downconversion_tasks
from salmon.uploader.cover_upload import resolve_cover_url
from salmon.uploader.dupe_checker import (
    NO_DUPE_SEARCH,
    check_existing_group,
    choose_source_flac,
    fetch_existing_group_candidates_in_background,
    generate_dupe_check_searchstrs,
    print_torrents,
    recheck_edition,
    resolve_existing_group,
)
from salmon.uploader.given_images import parse_spectral_urls, track_refusal, url_refusal
from salmon.uploader.metadata_review import edit_metadata, last_min_dupe_check, recheck_dupe
from salmon.uploader.preassumptions import confirm_group_upload, print_preassumptions, validate_skip_flac_source
from salmon.uploader.request_checker import check_requests
from salmon.uploader.seedbox import UploadManager
from salmon.uploader.spectrals import (
    SpectralUploads,
    check_spectrals,
    foreign_spectrals_refusal,
    generate_lossy_approval_comment,
    get_spectrals_path,
    make_way_for_spectrals,
    post_upload_spectral_check,
    remove_own_spectrals,
    specs_hosts_text,
    spectrals_in_background,
)
from salmon.uploader.staging import staged_source
from salmon.uploader.upload import (
    concat_track_data,
    upload_and_report,
)
from salmon.uploader.upload_checks import (
    _check_logs,
    _do_not_upload_refusal,
    _files_refusal,
    _lossy_master_without_spectrals,
    _warn_about_provenance,
)

if TYPE_CHECKING:
    from salmon.tagger.tagfile import TagFile
    from salmon.trackers.base import BaseGazelleApi
    from salmon.uploader.dupe_checker import GroupCandidatesFetch
    from salmon.uploader.spectrals import SpectralsInBackground


def validate_spectral_urls(ctx, param, value: tuple[str, ...]) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Parse --spectral-url: TRACK=URL[,URL] each, into (track, URLs) pairs in the order given.

    Raises:
        click.BadParameter: A value is malformed, or names a track twice.
    """
    try:
        return parse_spectral_urls(value)
    except ValueError as e:
        raise click.BadParameter(str(e)) from None


def validate_group_id(ctx, param, value: str | None) -> int | None:
    """Parse --group-id by the rule the prompts read a typed group ID by. None when not given.

    Raises:
        click.BadParameter: The value is not a number above 0 in ASCII digits.
    """
    if value is None:
        return None
    group_id = numeric_id(value.strip())
    if not group_id:
        raise click.BadParameter(f"{value!r} is not a group ID: give a number above 0, in the digits 0-9.")
    return group_id


@commandgroup.command()
@click.argument("path", type=AlbumPath(exists=True, file_okay=False))
@click.option(
    "--group-id",
    "-g",
    metavar="INTEGER",
    default=None,
    callback=validate_group_id,
    help="Group ID to upload torrent to",
)
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
    help=f"Uploading Choices: ({'/'.join(salmon.trackers.tracker_list)}). Name one, or several comma-separated or "
    "with -t repeated, to upload to exactly those, in that order, without being asked for another tracker. Without -t, "
    "salmon uses tracker.default_trackers in order, then offers the other trackers only if "
    "upload.multi_tracker_upload is on; with no list, it asks which tracker.",
)
@click.option("--request", "-r", default=None, help="Pass a request URL or ID")
@click.option(
    "--spectrals-after",
    "-a",
    is_flag=True,
    help="Assess / upload / report spectrals after torrent upload",
)
@click.option(
    "--cover-url",
    default=None,
    help="The URL of the cover, uploaded already to one of salmon's image hosts: used for every new group of the "
    "run instead of uploading the cover. Every tracker of the run must show that host's images.",
)
@click.option(
    "--spectral-url",
    "spectral_urls",
    multiple=True,
    callback=validate_spectral_urls,
    metavar="TRACK=URL[,URL]",
    help="A track's spectrals, uploaded already to one of salmon's image hosts: the Full image's URL, then the "
    "Zoom one's. TRACK is numbered as --spectrals numbers tracks. Repeat it for each track, in the order the "
    "description shows them. salmon then makes, shows and uploads no spectrals; it still asks whether the release is "
    "lossy mastered, unless --lossy or --not-lossy says.",
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
    "--skip-search",
    is_flag=True,
    help="With --source-url naming a store salmon scrapes, take the metadata from that URL without searching "
    "the stores.",
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
    "--conversions",
    callback=validate_conversions,
    help="Makes these conversions after the FLAC, without asking: a comma-separated list of "
    f"{', '.join(CONVERSION_KINDS)}, or none, or all. Overrides upload.conversions for this run.",
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
    cover_url: str | None,
    spectral_urls: tuple[tuple[int, tuple[str, ...]], ...],
    auto_rename: bool,
    skip_up: bool,
    scene: bool,
    source_url: str | None,
    skip_search: bool,
    skip_initial_review: bool,
    apply_ai_suggestions: bool,
    yyy: bool,
    skip_mqa: bool,
    skip_log_check: bool,
    skip_integrity_check: bool,
    essential_only: bool,
    conversions: tuple[str, ...] | None,
    dry_run: bool,
) -> None:
    """Command to upload an album folder to a Gazelle Site.

    Exits with 0 when everything the run set out to upload went up (a tracker you skipped at its group question, or
    did not pick when offered, counts as done).
    1 when no tracker can have taken an upload: a retry is safe, keep the album. 2 on a usage error. 3 when a tracker
    may have taken one, or refused one: a torrent is up, an upload was refused or an outcome is unknown. Don't retry
    the whole run, and don't delete the album. A failed seedbox copy or client add is a 3.
    """
    options = UpOptions(
        path=path,
        trackers=trackers,
        trackers_named=not isinstance(trackers, salmon.trackers.UnnamedTrackers),
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
        cover_url=cover_url.strip() if cover_url else None,
        spectral_urls=spectral_urls,
        auto_rename=auto_rename,
        skip_up=skip_up,
        scene=scene,
        source_url=source_url,
        skip_search=skip_search,
        skip_initial_review=skip_initial_review,
        apply_ai_suggestions=apply_ai_suggestions,
        skip_mqa=skip_mqa,
        skip_log_check=skip_log_check,
        skip_integrity_check=skip_integrity_check,
        essential_only=essential_only,
        conversions=conversions,
    )
    if (error := options.usage_error(dry_run)) is not None:
        raise click.UsageError(error)
    record.note_up_command()
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
        trackers_named: Whether -t named the trackers: the run uploads to exactly those. False for the config's
            default_trackers or the tracker asked for (``UnnamedTrackers``): the run then offers the other configured
            trackers after them, and --skip-flac-upload takes the first.
        source: The media source, as ``validate_source`` gives it.
        group_id: The group to upload into.
        encoding: The lossy encoding, as ``validate_encoding`` gives it.
        request: The request to fill, a URL or an ID, checked against the tracker by ``run_up``.
        conversions: The conversion kinds to make after a FLAC, without asking (``--conversions``); None follows
            upload.conversions, which is asked about.
        cover_url: The cover's URL, uploaded already (``--cover-url``): every new group of the run takes it, and the
            cover is not uploaded.
        spectral_urls: The spectrals' URLs, uploaded already (``--spectral-url``): (track, (Full URL, Zoom URL)) in
            the order the description shows them. No spectrals are made or uploaded.
    """

    path: str
    trackers: tuple[str, ...]
    source: str | None
    trackers_named: bool = True
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
    skip_search: bool = False
    skip_initial_review: bool = False
    apply_ai_suggestions: bool = False
    skip_mqa: bool = False
    skip_log_check: bool = False
    skip_integrity_check: bool = False
    essential_only: bool = False
    conversions: tuple[str, ...] | None = None
    cover_url: str | None = None
    spectral_urls: tuple[tuple[int, tuple[str, ...]], ...] = ()

    def usage_error(self, dry_run: bool) -> str | None:
        """Why these options cannot go together, as ``salmon up`` says it; None when they can."""
        if (error := self._given_images_error()) is not None:
            return error
        if self.skip_flac_upload and self.group_id is None:
            return "--skip-flac-upload requires --group-id."
        if self.skip_flac_upload and self.request:
            return "--skip-flac-upload cannot be used with --request."
        if self.skip_flac_upload and self.spectrals_after:
            return "--skip-flac-upload cannot be used with --spectrals-after."
        if self.essential_only and self.scene:
            return "--essential-only and --scene cannot be used together."
        if self.skip_flac_upload and self.trackers[1:] and self.trackers_named:
            return "--skip-flac-upload uploads to one tracker: give -t a single tracker."
        if dry_run and self.spectrals_after:
            return (
                "--dry-run cannot be used with --spectrals-after: that step edits the uploaded torrent, and a dry run "
                "uploads none."
            )
        return None

    def _given_images_error(self) -> str | None:
        """Why --cover-url or --spectral-url cannot be used for this run, None when they can; see given_images."""
        if not self.cover_url and not self.spectral_urls:
            return None
        if self.spectral_urls and self.spectrals:
            return "--spectral-url cannot be used with --spectrals: the URLs given are the spectrals."
        if self.spectral_urls and self.spectrals_after:
            return "--spectral-url cannot be used with --spectrals-after: the URLs given are the spectrals."
        # Every tracker the run may reach, as the run itself works them out: --skip-flac-upload uploads to the first.
        trackers = (
            list(self.trackers[:1])
            if self.skip_flac_upload
            else _trackers_for_run(
                self.trackers[0], None, _reachable_trackers(list(self.trackers), not self.trackers_named)
            )
        )
        if self.cover_url and (reason := url_refusal(self.cover_url, trackers)) is not None:
            return f"--cover-url: {reason}."
        if self.spectral_urls:
            tracks = (track for track, _urls in self.spectral_urls)
            if (reason := track_refusal(tracks, get_audio_files(self.path))) is not None:
                return f"--spectral-url: {reason}."
            for _track, urls in self.spectral_urls:
                for url in urls:
                    if (reason := url_refusal(url, trackers, spectrals=True)) is not None:
                        return f"--spectral-url: {reason}."
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
    # --skip-flac-upload uploads to one tracker: the first of the config's list.
    trackers = options.trackers[:1] if options.skip_flac_upload else options.trackers
    gazelle_site = salmon.trackers.get_class(trackers[0])()
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
        # The run's spectrals, uploaded already: --lossy is reported with them.
        spectral_url_tracks=tuple(track for track, _urls in options.spectral_urls),
    )
    flac_group = None
    if options.group_id:
        group = await confirm_group_upload(gazelle_site, options.group_id, options.source)
        if options.skip_flac_upload:
            flac_group = group
    source_url = without_tracking(options.source_url.strip()) if options.source_url else options.source_url
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
        skip_search=options.skip_search,
        apply_ai_suggestions=options.apply_ai_suggestions,
        trackers=list(trackers),
        offer_others=not options.trackers_named,
        conversions=options.conversions,
        given_cover_url=options.cover_url,
        given_spectral_urls={track: list(urls) for track, urls in options.spectral_urls} or None,
    )
    if dryrun.active():
        dryrun.say(f"done. Nothing was sent, and {path} is unchanged.")


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
    conversions: tuple[str, ...] | None = None,
    skip_search: bool = False,
    offer_others: bool = False,
    given_cover_url: str | None = None,
    given_spectral_urls: dict[int, list[str]] | None = None,
) -> None:
    """Upload an album folder to Gazelle Site.

    With multi_tracker_upload, offer the choice to upload to another tracker after completion, unless `trackers`
    names them and not `offer_others`.

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
        offer_others: `trackers` were not named with -t (the config's default_trackers, or the tracker asked for):
            after them, offer the other configured trackers, as configured.
        conversions: The conversion kinds to make after a FLAC, without asking (see ``conversion_kind``). None
            follows upload.conversions, which is asked about.
        skip_search: Take the metadata from source_url without searching the stores, when a metadata source
            scrapes it.
        given_cover_url: The cover's URL, uploaded already: every new group takes it, and the cover is not uploaded.
            Checked against the run's trackers by the caller (``UpOptions.usage_error``).
        given_spectral_urls: The spectrals' URLs, uploaded already, by track number as `spectrals` numbers them, in
            the description's order: no spectrals are made, shown or uploaded. Checked by the caller too.
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
        original = path if staged != path else None
        # Before the upload's tracker requests (with -g, run_up has fetched the group already): the question covers
        # the conversions the run makes too.
        if (refusal := await foreign_spectrals_refusal(path, staged)) is not None:
            return click.secho(f"\n{refusal}", fg="red", bold=True)
        remove_own_spectrals(staged)
        # An ALAC album is uploaded as FLAC: converted in the folder the upload works on, before its checks read it.
        if (refusal := await convert_alac(path, staged)) is not None:
            return click.secho(f"\n{refusal}", fg="red", bold=True)
        result = await _upload_staged(
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
            original=original,
            conversion=conversion,
            trackers=trackers,
            conversions=conversions,
            skip_search=skip_search,
            offer_others=offer_others,
            given_cover_url=given_cover_url,
            given_spectral_urls=given_spectral_urls,
        )
        # Once the run is done: an abort after the folder rename keeps the source folder, so a re-run can use it.
        final, uploaded, aborted, only_here = result or (None, False, False, None)
        await remove_source_folder(path, final, original, uploaded=uploaded, aborted=aborted, only_here=only_here)


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


def _another_can_follow(first_tracker: str, flac_group: dict[str, Any] | None, reachable: list[str] | None) -> bool:
    """Whether the run can go on to a tracker after first_tracker."""
    return len(_trackers_for_run(first_tracker, flac_group, reachable)) > 1


def _reachable_trackers(trackers: list[str] | None, offer_others: bool) -> list[str] | None:
    """Every tracker a run given these trackers may upload to, in order; None when it is only known as it goes.

    Trackers not named with -t (`offer_others`: the config's default_trackers, or the one asked for) are followed by
    the other configured trackers the run offers, with multi_tracker_upload.
    """
    if not trackers or not offer_others or not cfg.upload.multi_tracker_upload:
        return trackers
    return [*trackers, *(code for code in salmon.trackers.tracker_list if code not in trackers)]


async def _upload_staged(  # noqa: C901
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
    original: str | None = None,
    conversion: dict[str, Any] | None = None,
    skip_search: bool = False,
    trackers: list[str] | None = None,
    conversions: tuple[str, ...] | None = None,
    offer_others: bool = False,
    given_cover_url: str | None = None,
    given_spectral_urls: dict[int, list[str]] | None = None,
) -> tuple[str, bool, bool, list[str] | None] | None:
    """Run upload() on a folder that is safe to change; see upload() for the arguments.

    Returns:
        The folder the run ended with, whether anything was uploaded, whether the run was aborted after an
        upload, and, with remove_source_dir on, the files of path that folder had no copy of before
        remove_local_after_copy could remove it (see files_only_in); None when it stopped early.

    Args:
        conversion: What the folder was converted from, when a converter made it: the upload describes it.
        rename_into: The directory the renamed folder goes into, instead of download_directory.
        original: The folder the user gave, when path is a copy of it: the folder rename says why it is kept, and
            "delete" deletes only the copy.
    """
    remove_downloaded_cover_image = scene or cfg.image.remove_auto_downloaded_cover_image
    # Every tracker the run may reach, in order: those uploaded to without asking, then those it may offer.
    reachable = _reachable_trackers(trackers, offer_others)
    # The album folder and every conversion the run makes, on any tracker's turn, are checked against it.
    max_path_length = _max_path_length_for_run(
        gazelle_site, _trackers_for_run(gazelle_site.site_code, flac_group, reachable)
    )
    if not source:
        source = await _prompt_source(detect_source(path))
    audio_info = gather_audio_info(path)
    hybrid = check_hybrid(audio_info)
    # Before any tag is rewritten: standardize_tags saves a file whose tags use an alias. Printed further down.
    # A salmon conversion carries its source's tags, which describe the source's audio, not its own: no news.
    provenance = gather_provenance(path) if conversion is None else None
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
    # The release the first tracker's group was picked for, as it was then: None for a group given with -g.
    weighed_against: dict[str, Any] | None = None

    dupe_searchstrs: list[str] = []
    # A release the first tracker's list forbids gets no group search there, so no prompt to pick a group. The
    # review may change the names: the list is checked again after it.
    tags_refusal = _do_not_upload_refusal(gazelle_site.site_code, rls_data)
    # The files never change in the review. With --skip-flac-upload only transcodes go up: no FLAC to refuse.
    files_refused = flac_group is None and _files_refusal(gazelle_site.site_code, rls_data, audio_info)
    if group_id is None:
        # Left empty for a listed release: if the review takes it off the list, recheck_dupe then searches.
        searchstrs = dupe_searchstrs = (
            []
            if tags_refusal or files_refused
            else generate_dupe_check_searchstrs(rls_data["artists"], rls_data["title"], rls_data["catno"])
        )

    source_folder = path  # The folder the run started from: the metadata step renames path into a new folder
    search_failed = False
    try:
        if files_refused and not _another_can_follow(gazelle_site.site_code, flac_group, reachable):
            # Nothing the checks or the review change could let these files go up. Exit(1), not Abort: the run
            # failed, and _upload_staged ends an Abort as a user's no.
            raise click.exceptions.Exit(1)
        # The search for an existing group only reads from the tracker, so it runs in the background during
        # the MQA, upconvert and log checks below, and what it found is shown once they are done.
        async with fetch_existing_group_candidates_in_background(
            gazelle_site, dupe_searchstrs, rls_data["title"]
        ) as group_fetch:
            # The spectrals only read the album, so they are made in the background during the checks and the
            # group question, and shown at the spectral step. None are made with --spectrals-after or given URLs.
            async with (
                nullcontext()
                if spectrals_after or given_spectral_urls
                else spectrals_in_background(path, audio_info, spectrals, rls_data["format"])
            ) as spectrals_generation:
                await _check_files(
                    path,
                    source,
                    rls_data,
                    provenance,
                    skip_mqa=skip_mqa,
                    skip_up=skip_up,
                    skip_log_check=skip_log_check,
                )

                if group_fetch is not None:
                    group_id, weighed_against, search_failed = await _take_group_search(
                        gazelle_site,
                        group_fetch,
                        group_id,
                        dupe_searchstrs,
                        rls_data,
                        flac_group=flac_group,
                        reachable=reachable,
                    )

                spectral_ids, lossy_master = await _spectrals_before_upload(
                    gazelle_site,
                    path,
                    audio_info,
                    rls_data,
                    lossy,
                    spectrals,
                    spectrals_generation,
                    spectrals_after=spectrals_after,
                    given_spectral_urls=given_spectral_urls,
                    flac_group=flac_group,
                    reachable=reachable,
                )

            path, metadata, tags, audio_info, source_url = await _review_until_confirmed(
                path,
                tags,
                audio_info,
                rls_data,
                source,
                source_url,
                spectral_ids,
                skip_search=skip_search,
                recompress=recompress,
                auto_rename=auto_rename,
                skip_integrity_check=skip_integrity_check,
                essential_only=essential_only,
                skip_initial_review=skip_initial_review,
                apply_ai_suggestions=apply_ai_suggestions,
                rename_into=rename_into,
                original=original,
                max_path_length=max_path_length,
            )

            # Before anything is made or sent for the first tracker: its group, spectrals, cover and upload.
            first_listed = (
                search_failed
                or files_refused
                or _do_not_upload_refusal(gazelle_site.site_code, metadata, said=tags_refusal) is not None
            )
            if not group_id and not first_listed:
                group_id = await recheck_dupe(gazelle_site, searchstrs, metadata)
                click.echo()
            elif group_id and flac_group is None and not first_listed:
                # The review may have moved the release into an edition the group holds. With
                # --skip-flac-upload, choose_source_flac below weighs the reviewed edition instead.
                group_id = await recheck_edition(gazelle_site, group_id, metadata, weighed_against)
            # From here on, the review may have changed the artists, title or catno, so search strings and
            # our title come from the reviewed metadata, not the pre-review rls_data.
            searchstrs = generate_dupe_check_searchstrs(metadata["artists"], metadata["title"], metadata["catno"])
            our_title = metadata["title"]
            track_data = concat_track_data(tags, audio_info)
            if flac_group is not None:
                # Matched on the reviewed metadata, so an edited year or edition title moves the pick.
                source_flac = await choose_source_flac(flac_group, metadata)
                if source_flac is None:
                    raise click.Abort
            if not scene:
                await _prepare_pictures(path, metadata, remove_downloaded_cover_image)
    except click.Abort:
        return click.secho("\nAborting upload...", fg="red")
    except AbortAndDeleteFolder:
        return _delete_after_abort(path, source_folder, original, flac_group)

    # Uploaded once per specs host; the files are deleted once no tracker of the run can need them.
    spectral_uploads = SpectralUploads(
        path, _trackers_for_run(gazelle_site.site_code, flac_group, reachable), given=given_spectral_urls
    )
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

        # Shallow copy to avoid errors on multiple uploads in one session. Trackers named with -t, or the config's
        # default_trackers, are uploaded to in their order, without asking; the others are offered.
        remaining_gazelle_sites = list(reachable or salmon.trackers.tracker_list)
        # Whether the run goes on after a tracker: always for named ones, until none is left.
        go_on = bool(reachable) or cfg.upload.multi_tracker_upload
        tracker = gazelle_site.site_code
        requested = request_id  # What --request gave, if anything: check_requests only looks when it gave none
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
        # Trackers whose upload failed or was skipped, or that are missing a conversion the run wanted: the folder is
        # needed to retry them. A first tracker refused before the loop is one of them.
        failed_trackers: list[str] = [gazelle_site.site_string] if first_listed else []
        finished = False  # The loop ended by itself: no abort, no exception
        aborted = False
        chose_to_skip = 0  # Of failed_trackers, those the user skipped: the run still finishes.
        flac_url = f"{gazelle_site.base_url}/torrents.php?torrentid={source_flac['id']}" if source_flac else None

        try:
            while True:
                # Loop until we don't want to upload to any more sites.
                if not tracker:
                    # After a tracker whose Do-Not-Upload list forbids the release: the run may have none left.
                    if flac_url or not remaining_gazelle_sites or not go_on:
                        break
                    if next_named := next((code for code in remaining_gazelle_sites if code in (trackers or ())), None):
                        tracker = next_named
                        click.secho(f"\nNext tracker: {tracker}", fg="magenta")
                    else:
                        click.secho("\nWould you like to upload to another tracker? ", fg="magenta", nl=False)
                        tracker = await salmon.trackers.choose_tracker(remaining_gazelle_sites)
                    if not tracker:
                        click.secho("\nDone with this release.", fg="green")
                        break
                    # A request is of the tracker it was given or found for, whatever became of that tracker's turn.
                    if request_id is not None:
                        if request_id == requested:
                            click.secho(
                                f"\nNot filling request {request_id}: it is {gazelle_site.site_code}'s.", fg="yellow"
                            )
                        request_id = None
                    gazelle_site = salmon.trackers.get_class(tracker)()
                    # Before its dupe check, which may ask which group to upload into.
                    if _do_not_upload_refusal(tracker, metadata) is not None or (
                        flac_group is None and _files_refusal(tracker, metadata, audio_info)
                    ):
                        failed_trackers.append(gazelle_site.site_string)
                        remaining_gazelle_sites.remove(tracker)
                        tracker = None
                        continue

                    if not searchstrs and await interaction.assume_defaults():
                        # As recheck_dupe for the first tracker (#931): with -yyy, nobody checks the tracker by hand.
                        click.secho(
                            f"\n{NO_DUPE_SEARCH} Not uploading to {gazelle_site.site_string} with -yyy "
                            "(upload.yes_all): run without -yyy, and with upload.yes_all off, to choose its group.",
                            fg="red",
                            bold=True,
                        )
                        failed_trackers.append(gazelle_site.site_string)
                        remaining_gazelle_sites.remove(tracker)
                        tracker = None
                        continue

                    click.secho(f"Uploading to {gazelle_site.base_url}", fg="cyan", bold=True)
                    # A torrent already seeds from the folder: never offer to delete it. Once one is up, a tracker
                    # can be skipped (one that already has the release, say) and the run still finishes.
                    # Matched on the reviewed metadata: the review may have changed the artists, title or year.
                    try:
                        group_id = await check_existing_group(
                            gazelle_site,
                            searchstrs,
                            offer_deletion=False,
                            our_title=our_title,
                            release=metadata,
                            offer_skip=bool(uploaded),
                        )
                    except (RequestError, SkipTracker) as e:
                        # Like a failed upload: skip this tracker, and offer the next one.
                        if isinstance(e, SkipTracker):
                            chose_to_skip += 1
                            click.secho(f"\nSkipping upload to {gazelle_site.site_string}.", fg="yellow", bold=True)
                        else:
                            click.secho(f"\nUpload to {gazelle_site.site_string} failed: {e}", fg="red", bold=True)
                            print_certificate_hint(e)
                        failed_trackers.append(gazelle_site.site_string)
                        remaining_gazelle_sites.remove(tracker)
                        tracker = None
                        if not remaining_gazelle_sites or not go_on:
                            break
                        continue

                remaining_gazelle_sites.remove(tracker)

                # Handle cover image for this tracker
                proceed, cover_url = await resolve_cover_url(
                    gazelle_site,
                    group_id,
                    cover_urls,
                    path,
                    metadata["cover"],
                    remove_downloaded_cover_image,
                    given_url=given_cover_url,
                )
                if not proceed:
                    # Like a failed upload: skip this tracker, and offer the next one.
                    click.secho(f"\nSkipping upload to {gazelle_site.site_string}.", fg="red", bold=True)
                    failed_trackers.append(gazelle_site.site_string)
                    tracker = None
                    if not remaining_gazelle_sites or not go_on:
                        break
                    continue

                if not spectrals_after:
                    # Uploaded to this tracker's specs host, unless a tracker using the same host already did.
                    spectral_urls = await spectral_uploads.urls_for(gazelle_site.site_code, spectral_ids)

                try:
                    if not flac_url and not request_id and cfg.upload.requests.check_requests:
                        request_id = await check_requests(gazelle_site, searchstrs)

                    held: set[str] = set()
                    if flac_url and source_flac is not None:
                        click.secho(f"\nNot uploading the FLAC: transcoding from {flac_url}", fg="yellow")
                        url = flac_url
                        held = _held_conversions(flac_group or {}, metadata, source_flac, rls_data, track_data)
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

                        if not group_id:
                            # Conversions sent with no group id would each start a new group beside this one.
                            click.secho(
                                f"\nNo group id came back for {url}: no conversions are offered for it.", fg="yellow"
                            )
                            if _applicable_conversions(
                                get_downconversion_options(rls_data, track_data, quiet=True), conversions
                            ):
                                failed_trackers.append(gazelle_site.site_string)
                        elif not dryrun.active():
                            group = await print_torrents(gazelle_site, group_id, highlight_torrent_id=torrent_id)
                            # Read from the group just printed: no request of its own.
                            held = _held_conversions(
                                group, metadata, {"id": torrent_id}, rls_data, track_data, uploaded=True
                            )

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

                    # Nothing to convert (an MP3 upload) asks nothing, nor does an upload whose group is not known.
                    conversion_options = (
                        get_downconversion_options(rls_data, track_data, quiet=True) if group_id else []
                    )
                    if conversion_options:
                        selected_tasks = await choose_conversions(
                            rls_data, track_data, conversion_options, held, conversions, ask_first=not flac_url
                        )
                        if selected_tasks:
                            display_names = [task["name"] for task in selected_tasks]
                            click.secho(
                                f"\nSelected formats for downconversion: {', '.join(display_names)}",
                                fg="green",
                                bold=True,
                            )

                            # Execute downconversion tasks
                            before = len(uploaded)
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
                                max_path_length=max_path_length,
                            )
                            if len(uploaded) - before < len(selected_tasks):
                                failed_trackers.append(gazelle_site.site_string)
                except RequestError as e:
                    click.secho(f"\nUpload to {gazelle_site.site_string} failed: {e}", fg="red", bold=True)
                    print_certificate_hint(e)
                    failed_trackers.append(gazelle_site.site_string)
                    if isinstance(e, UnknownOutcomeError):
                        record.note_unknown_outcome(e)

                tracker = None
                if flac_url or not remaining_gazelle_sites or not go_on:
                    click.secho("\nDone uploading this release.", fg="green")
                    break
            finished = True

        except click.Abort:
            if not uploaded or dryrun.active():
                raise
            # What is up stays up and is seeded below: the run only stops offering more.
            aborted = True
            click.secho("\nAborting: nothing more is uploaded. Already uploaded:", fg="red")
            for line in uploaded:
                click.echo(f"  {line}")
        finally:
            await seedbox_uploader.execute_upload()
            # Before remove_local_after_copy may remove the renamed folder: the source is compared with it (#967).
            only_here = files_only_in(source_folder, path) if cfg.upload.formatting.remove_source_dir else None
            # Only a run that ended by itself with every upload done: a failed tracker needs the folder to retry,
            # and an abort or an error leaves the user deciding what is next.
            if failed_trackers:
                seedbox_uploader.remove_local_copies(
                    f"the upload to {', '.join(failed_trackers)} failed or was skipped"
                )
            elif not finished:
                seedbox_uploader.remove_local_copies("the run stopped before it finished")
            else:
                seedbox_uploader.remove_local_copies()
    finally:
        await spectral_uploads.close()
    record.note_complete(finished and len(failed_trackers) == chose_to_skip and not record.has_unknown_outcome())
    return path, bool(uploaded), aborted, only_here


async def _check_files(
    path: str,
    source: str,
    rls_data: dict[str, Any],
    provenance: dict[str, Any] | None,
    *,
    skip_mqa: bool,
    skip_up: bool,
    skip_log_check: bool,
) -> None:
    """Check the files before the group question: MQA, an upconvert, the logs, and the tags' provenance markers."""
    if not skip_mqa:
        all_files = cfg.upload.mqa_check_all_tracks
        checked = "every FLAC file" if all_files else "first FLAC file only"
        click.secho(f"Checking for MQA release ({checked})", fg="cyan", bold=True)
        await mqa_test(path, all_files=all_files)
        click.secho("No MQA release detected", fg="green")

    if rls_data["encoding"] == "24bit Lossless" and not skip_up:
        if not await interaction.assume_defaults():
            if await interaction.confirm(
                click.style("\n24bit detected. Do you want to check whether might be upconverted?", fg="magenta"),
                default=True,
            ):
                await upload_upconvert_test(path)
        else:
            await upload_upconvert_test(path)

    if source == "CD" and not skip_log_check:
        await _check_logs(path)

    if provenance is not None:
        _warn_about_provenance(provenance)


async def _take_group_search(
    gazelle_site: "BaseGazelleApi",
    group_fetch: "GroupCandidatesFetch",
    group_id: int | None,
    dupe_searchstrs: list[str],
    rls_data: dict[str, Any],
    *,
    flac_group: dict[str, Any] | None,
    reachable: list[str] | None,
) -> tuple[int | None, dict[str, Any] | None, bool]:
    """Ask which group to upload into, from what the background search found.

    Returns:
        The group (group_id unchanged when the search failed), the release as it was when the group was picked (None
        when the search failed), and whether the search failed: the first tracker is then skipped.
    """
    weighed_against: dict[str, Any] | None = None
    search_failed = False
    try:
        results, recent_uploads = await group_fetch.result()
    except RequestError as e:
        # Skipped like a first tracker that refuses the release, when another can follow.
        if not _another_can_follow(gazelle_site.site_code, flac_group, reachable):
            click.secho(f"\nCould not search {gazelle_site.site_string} for dupes: {e}", fg="red", bold=True)
            print_certificate_hint(e)
            raise click.exceptions.Exit(1) from None
        click.secho(
            f"\nCould not search {gazelle_site.site_string} for dupes ({e}): skipping it.",
            fg="red",
            bold=True,
        )
        print_certificate_hint(e)
        search_failed = True
    else:
        group_id = await resolve_existing_group(
            gazelle_site, dupe_searchstrs, results, recent_uploads, release=rls_data
        )
        # A copy: picking scraped metadata edits rls_data in place (it is combine_metadatas' base).
        weighed_against = dict(rls_data)
    return group_id, weighed_against, search_failed


async def _spectrals_before_upload(
    gazelle_site: "BaseGazelleApi",
    path: str,
    audio_info: dict[str, Any],
    rls_data: dict[str, Any],
    lossy: bool | None,
    spectrals: tuple[int, ...],
    spectrals_generation: "SpectralsInBackground | None",
    *,
    spectrals_after: bool,
    given_spectral_urls: dict[int, list[str]] | None,
    flac_group: dict[str, Any] | None,
    reachable: list[str] | None,
) -> tuple[dict[int, str] | None, bool]:
    """Show the spectrals, or take the ones given by URL, and ask whether the release is lossy mastered.

    With --spectrals-after, all of that waits for the first upload.

    Returns:
        The spectral images by track, and whether the release is lossy mastered.
    """
    spectral_ids = None
    lossy_master: bool = False
    if spectrals_after:
        # We tell the uploader not to worry about it being lossy until later.
        pass
    elif given_spectral_urls:
        # As check_spectrals would: salmon's spectrals from an earlier run must not go into the torrents.
        make_way_for_spectrals(get_spectrals_path(path))
        lossy_master = await _lossy_master_without_spectrals(lossy, rls_data["format"])
        # Numbered as --spectrals numbers tracks; the files' names go into the description.
        audio_files = get_audio_files(path)
        spectral_ids = {track: audio_files[track - 1] for track in given_spectral_urls}
    else:
        lossy_result, spectral_ids = await check_spectrals(
            path,
            audio_info,
            lossy,
            spectrals,
            format=rls_data["format"],
            hosts=specs_hosts_text(_trackers_for_run(gazelle_site.site_code, flac_group, reachable)),
            generation=spectrals_generation,
        )
        lossy_master = lossy_result if lossy_result is not None else False
    return spectral_ids, lossy_master


async def _review_until_confirmed(
    path: str,
    tags: dict[str, "TagFile"],
    audio_info: dict[str, Any],
    rls_data: dict[str, Any],
    source: str,
    source_url: str | None,
    spectral_ids: dict[int, str] | None,
    *,
    skip_search: bool,
    recompress: bool,
    auto_rename: bool,
    skip_integrity_check: bool,
    essential_only: bool,
    skip_initial_review: bool,
    apply_ai_suggestions: bool,
    rename_into: str | None,
    original: str | None,
    max_path_length: int,
) -> tuple[str, dict[str, Any], dict[str, "TagFile"], dict[str, Any], str | None]:
    """Pick the metadata and review it until the user uploads, picking again from the results if asked.

    Returns:
        The folder, the metadata, the tags and the audio info as the review left them, and the source URL.
    """
    run_source_url = source_url
    metadata_search = MetadataSearch()
    metadata, picked_source_url = await get_metadata(
        path, tags, rls_data, skip_search=skip_search, skip_search_url=source_url, search=metadata_search
    )
    while True:
        # Each pick's starred URL replaces the run's --source-url; a pick with none keeps the run's.
        source_url = run_source_url if picked_source_url is None else picked_source_url
        if picked_source_url is not None:
            click.secho(f"New Source URL: {source_url}", fg="yellow")
        try:
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
                original=original,
                max_path_length=max_path_length,
                rls_type_hint=suggest_release_type(
                    rls_data.get("title"), [info.get("duration") or 0 for info in audio_info.values()]
                ),
                offer_other_results=metadata_search.results is not None,
            )
            break
        except PickOtherResults:
            # Raised at a review before anything was written to the files.
            metadata, picked_source_url = await pick_metadata(metadata_search, rls_data, path, len(tags))
    return path, metadata, tags, audio_info, source_url


async def _prepare_pictures(path: str, metadata: dict[str, Any], remove_downloaded_cover_image: bool) -> None:
    """Check the files' embedded pictures, and embed a missing cover when configured to."""
    # Before any torrent or transcode is made from the folder, so they all get the same files.
    check_embedded_pictures(path)
    if cfg.image.embed_missing_cover:
        # A missing cover is downloaded from the metadata first, as the per-tracker step used to.
        cover_path, downloaded = None, False
        if metadata["cover"] and has_flac_without_picture(path) and not get_cover_from_path(path):
            cover_path, downloaded = await download_cover_if_nonexistent(path, metadata["cover"])
        embed_missing_cover(path, cover_path)
        if downloaded and remove_downloaded_cover_image and cover_path:
            # Not in the torrents; the cover upload downloads it again.
            os.remove(cover_path)


def _delete_after_abort(path: str, source_folder: str, original: str | None, flac_group: dict[str, Any] | None) -> None:
    """Delete the folder an aborted run worked on, as the user asked, unless it must be kept."""
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
    if original is not None:
        click.secho(
            f"\nDeleting the copy the upload worked on. {original} is kept.",
            fg="yellow",
            bold=True,
        )
    elif cfg.upload.formatting.remove_source_dir and path != source_folder and os.path.isdir(source_folder):
        # The answer deletes the renamed folder only: the source is removed when a run ends, and this one aborts
        click.secho(
            f"\nremove_source_dir is on, but {source_folder} is kept: remove it yourself if you meant the album too.",
            fg="yellow",
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
