import os
import platform
import shutil
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

import anyio
import asyncclick as click
import pyperclip
from mutagen import MutagenError

import salmon.trackers
from salmon import cfg
from salmon.checks import mqa_test
from salmon.checks.integrity import resolve_integrity_for_upload
from salmon.checks.logs import check_log_cambia
from salmon.checks.upconverts import upload_upconvert_test
from salmon.common import commandgroup, tagify
from salmon.config.image_hosts import cover_refusal
from salmon.constants import ENCODINGS, FORMATS, SOURCES, TAG_ENCODINGS
from salmon.converter.downconverting import (
    convert_folder,
    generate_conversion_description,
)
from salmon.converter.transcoding import (
    generate_transcode_description,
    transcode_folder,
)
from salmon.errors import (
    AbortAndDeleteFolder,
    CRCMismatchError,
    EditedLogError,
    InvalidMetadataError,
    LogCheckSkipped,
    RequestError,
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
from salmon.tagger.review import review_metadata
from salmon.tagger.tags import check_tags, gather_tags, standardize_tags
from salmon.trackers.red import RedApi
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
    check_spectrals,
    generate_lossy_approval_comment,
    get_spectrals_path,
    handle_spectrals_upload_and_deletion,
    post_upload_spectral_check,
    report_lossy_master,
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
    callback=salmon.trackers.validate_tracker,
    help=f"Uploading Choices: ({'/'.join(salmon.trackers.tracker_list)})",
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
    tracker: str,
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
) -> None:
    """Command to upload an album folder to a Gazelle Site."""
    if skip_flac_upload and group_id is None:
        raise click.UsageError("--skip-flac-upload requires --group-id.")
    if skip_flac_upload and request:
        raise click.UsageError("--skip-flac-upload cannot be used with --request.")
    if skip_flac_upload and spectrals_after:
        raise click.UsageError("--skip-flac-upload cannot be used with --spectrals-after.")
    if essential_only and scene:
        raise click.UsageError("--essential-only and --scene cannot be used together.")
    if yyy:
        cfg.upload.yes_all = True
    gazelle_site = salmon.trackers.get_class(tracker)()
    if request:
        request = salmon.trackers.validate_request(gazelle_site, request)
        # This is isn't handled by click because we need the tracker sorted first.
    print_preassumptions(
        gazelle_site,
        path,
        group_id,
        source,
        lossy,
        spectrals,
        encoding,
        spectrals_after,
    )
    flac_group = None
    if group_id:
        group = await confirm_group_upload(gazelle_site, group_id, source)
        if skip_flac_upload:
            flac_group = group
    if source_url:
        source_url = source_url.strip()
    await upload(
        gazelle_site,
        path,
        group_id,
        source,
        lossy,
        spectrals,
        encoding,
        source_url=source_url,
        scene=scene,
        overwrite_meta=overwrite,
        recompress=compress,
        request_id=request,
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
    )


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
        with a None URL means the upload itself failed.
    """
    host = host or cfg.image.cover_uploader_for(tracker)
    if not cover_urls.get(host):
        cover_path, is_downloaded = await download_cover_if_nonexistent(path, cover_source)
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
    elif (host if host is not None else cfg.image.cover_uploader_for(gazelle_site.site_code)) == "red":
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
        host_input: str = await click.prompt(
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

    default_host = cfg.image.cover_uploader_for(tracker)
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
            if cfg.upload.yes_all:
                click.secho("Not uploading a new group without a cover image with --yes-all.", fg="red", bold=True)
                return False, None

            choice = await click.prompt(
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
                if not click.confirm(
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
) -> None:
    """Upload an album folder to Gazelle Site.

    Offer the choice to upload to another tracker after completion.

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
    """
    path = os.path.abspath(path)
    if flac_group is not None and (refusal := validate_skip_flac_source(path)):
        return click.secho(f"\n{refusal}", fg="red", bold=True)
    # The group's FLAC is most likely seeding from path, so with --skip-flac-upload everything works on a copy.
    # So does an album in library_dirs: see staged_source.
    with staged_source(path, scratch=flac_group is not None) as (staged, rename_into):
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
        )


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
) -> None:
    """Run upload() on a folder that is safe to change; see upload() for the arguments.

    Args:
        rename_into: The directory the renamed folder goes into, instead of download_directory.
        library_album: The folder path is a copy of, when that folder must be kept: "delete" deletes only the copy.
    """
    remove_downloaded_cover_image = scene or cfg.image.remove_auto_downloaded_cover_image
    if not source:
        source = await _prompt_source()
    audio_info = gather_audio_info(path)
    hybrid = check_hybrid(audio_info)
    if not scene:
        standardize_tags(path)
    tags = gather_tags(path)
    rls_data = construct_rls_data(
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
    if group_id is None:
        searchstrs = dupe_searchstrs = generate_dupe_check_searchstrs(
            rls_data["artists"], rls_data["title"], rls_data["catno"]
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
                if not cfg.upload.yes_all:
                    if click.confirm(
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

            if group_fetch is not None:
                results, recent_uploads = await group_fetch.result()
                group_id = await resolve_existing_group(gazelle_site, dupe_searchstrs, results, recent_uploads)

            spectral_ids = None
            lossy_master: bool = False
            if spectrals_after:
                # We tell the uploader not to worry about it being lossy until later.
                pass
            else:
                lossy_result, spectral_ids = await check_spectrals(
                    path, audio_info, lossy, spectrals, format=rls_data["format"]
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
            )

            if not group_id:
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

    lossy_comment = None
    if spectrals_after:
        spectral_urls = None
    else:
        if lossy_master:
            lossy_comment = await generate_lossy_approval_comment(source_url, list(track_data.keys()))
            click.echo()

        spectrals_path = get_spectrals_path(path)
        spectral_urls = await handle_spectrals_upload_and_deletion(spectrals_path, spectral_ids)
    if cfg.upload.requests.last_minute_dupe_check:
        await last_min_dupe_check(gazelle_site, searchstrs, our_title)

    # Shallow copy to avoid errors on multiple uploads in one session.
    remaining_gazelle_sites = list(salmon.trackers.tracker_list)
    tracker = gazelle_site.site_code
    torrent_id = None
    cover_url = None
    cover_urls: dict[str, str | None] = {}  # Uploaded cover URL per image host, reused across trackers

    seedbox_uploader = UploadManager()
    flac_url = f"{gazelle_site.base_url}/torrents.php?torrentid={source_flac['id']}" if source_flac else None

    try:
        while True:
            # Loop until we don't want to upload to any more sites.
            if not tracker:
                if spectrals_after and torrent_id:
                    # Here we are checking the spectrals after uploading to the first site
                    # if they were not done before.
                    lossy_master, lossy_comment, spectral_urls, spectral_ids = await post_upload_spectral_check(
                        gazelle_site, path, torrent_id, None, track_data, source, source_url, format=rls_data["format"]
                    )
                    spectrals_after = False
                click.secho("\nWould you like to upload to another tracker? ", fg="magenta", nl=False)
                tracker = await salmon.trackers.choose_tracker(remaining_gazelle_sites)
                if not tracker:
                    click.secho("\nDone with this release.", fg="green")
                    break
                gazelle_site = salmon.trackers.get_class(tracker)()

                click.secho(f"Uploading to {gazelle_site.base_url}", fg="cyan", bold=True)
                group_id = await check_existing_group(gazelle_site, searchstrs, our_title=our_title)

            remaining_gazelle_sites.remove(tracker)

            # Handle cover image for this tracker
            proceed, cover_url = await resolve_cover_url(
                gazelle_site, group_id, cover_urls, path, metadata["cover"], remove_downloaded_cover_image
            )
            if not proceed:
                # Like a failed upload: skip this tracker, and offer the next one.
                click.secho(f"\nSkipping upload to {gazelle_site.site_string}.", fg="red", bold=True)
                tracker = None
                if not remaining_gazelle_sites or not cfg.upload.multi_tracker_upload:
                    break
                continue

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
                        for option in get_downconversion_options(rls_data, track_data)
                    }
                    held = held_formats(flac_group or {}, metadata, source_flac, formats)
                else:
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
                    )

                    request_id = None

                    await print_torrents(gazelle_site, group_id, highlight_torrent_id=torrent_id)

                if (
                    flac_url
                    or cfg.upload.yes_all
                    or click.confirm(
                        click.style("\nWould you like to check downconversion options?", fg="magenta"),
                        default=True,
                    )
                ):
                    selected_tasks = await prompt_downconversion_choice(rls_data, track_data, held)
                    if selected_tasks:
                        display_names = [task["name"] for task in selected_tasks]
                        click.secho(
                            f"\nSelected formats for downconversion: {', '.join(display_names)}", fg="green", bold=True
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
                        )
            except RequestError as e:
                click.secho(f"\nUpload to {gazelle_site.site_string} failed: {e}", fg="red", bold=True)

            tracker = None
            if flac_url or not remaining_gazelle_sites or not cfg.upload.multi_tracker_upload:
                click.secho("\nDone uploading this release.", fg="green")
                break

    finally:
        await seedbox_uploader.execute_upload()


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
            review_metadata,
            skip_initial_review=skip_initial_review,
            apply_suggestions=apply_ai_suggestions,
        )
        if not metadata["scene"]:
            tag_files(path, tags, metadata, auto_rename)

        tags = await check_tags(path)
        if not metadata["scene"] and recompress:
            await recompress_path(path)
        path = rename_folder(path, metadata, auto_rename, parent=rename_into)
        if not metadata["scene"]:
            rename_files(path, tags, metadata, auto_rename, spectral_ids, source)
        await check_folder_structure(path, metadata["scene"], essential_only=essential_only)

        if not skip_integrity_check:
            await resolve_integrity_for_upload(path, scene=metadata["scene"], assume_yes=cfg.upload.yes_all)

        if cfg.upload.yes_all or click.confirm(
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
        return await check_existing_group(gazelle_site, new_searchstrs, our_title=metadata["title"])
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
        if not click.confirm(
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


def get_downconversion_options(rls_data, track_data):
    """
    Determine available downconversion options based on current format.
    Returns a list of downconversion tasks

    Tier hierarchy:
    1. 24bit 176.4 ~ 192 kHz
    2. 24bit 44.1 ~ 96 kHz
    3. 16bit 44.1 ~ 48 kHz
    4. mp3 320
    5. mp3 v0
    """
    if not track_data:
        return []

    # Get sample rate from first track
    sample_rate = next(iter(track_data.values()))["sample rate"]
    encoding = rls_data["encoding"]

    options = []

    # Tier 1: 24bit 176.4~192 kHz
    if encoding == "24bit Lossless" and sample_rate >= 176400:
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
    if encoding == "24bit Lossless" and sample_rate >= 44100:
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
    if cfg.upload.yes_all:
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
            choices = await click.prompt(
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
                if click.confirm(click.style("Confirm selection?", fg="magenta"), default=True):
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
    """

    base_path = path

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
                output_dir=cfg.directory.download_directory,
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
            await check_folder_structure(new_path, conversion_metadata["scene"])

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

            click.secho(f"  ✓ {task['name']} conversion completed", fg="green")

        elif task["action"] == "transcode":
            # Call transcode function
            click.secho(f"  Target encoding: {task['encoding']}", fg="white")

            # Execute transcoding
            transcoded_path = await transcode_folder(
                base_path, task["encoding"], output_dir=cfg.directory.download_directory
            )
            await anyio.sleep(0.1)

            # Update metadata for this transcode
            transcode_metadata = metadata.copy()
            transcode_metadata["format"], transcode_metadata["encoding"] = downconversion_format(task)
            transcode_metadata["encoding_vbr"] = {"320": False, "V0": True}[task["encoding"]]

            # Generate description for transcode
            description = generate_transcode_description(base_url, task["encoding"])
            click.secho(f"  Generated description: {description[:100]}...", fg="blue")
            await check_folder_structure(transcoded_path, transcode_metadata["scene"])

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

    Returns:
        Tuple of (torrent_id, group_id, torrent_path, torrent_content, url).
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

    # Generate URL
    url = f"{gazelle_site.base_url}/torrents.php?torrentid={torrent_id}"

    torrent_content.comment = url
    torrent_content.write(torrent_path, overwrite=True)

    # Display success message
    click.secho(
        f"Successfully uploaded {url} ({os.path.basename(path)}).",
        fg="green",
        bold=True,
    )

    # Copy URL to clipboard
    if cfg.upload.description.copy_uploaded_url_to_clipboard:
        pyperclip.copy(url)

    # Add to seedbox upload queue
    if cfg.upload.upload_to_seedbox:
        click.secho("Add uploading task.", fg="green")
        # Check if it's a FLAC file
        is_flac = metadata.get("format", "").upper() == "FLAC"
        site_code = gazelle_site.site_code
        seedbox_uploader.add_upload_task(path, task_type="folder", is_flac=is_flac, site_code=site_code)
        seedbox_uploader.add_upload_task(
            torrent_path, task_type="seed", is_flac=is_flac, folder=path, site_code=site_code
        )

    return torrent_id, group_id, torrent_path, torrent_content, url


def convert_genres(genres):
    """Convert the weirdly spaced genres to RED-compliant genres."""
    return ",".join(t for t in (tagify(g) for g in genres) if t)


async def _prompt_source():
    click.echo(f"\nValid sources: {', '.join(SOURCES.values())}")
    while True:
        sauce = await click.prompt(
            click.style("What is the source of this release? [a]bort", fg="magenta"),
            default="",
        )
        try:
            return SOURCES[sauce.lower()]
        except KeyError:
            if sauce.lower().startswith("a"):
                raise click.Abort from None
            click.secho(f"{sauce} is not a valid source.", fg="red")
