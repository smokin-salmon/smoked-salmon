import os
import platform
import re
import shutil
import time
from typing import TYPE_CHECKING, Any

import asyncclick as click
import pyperclip

import salmon.trackers
from salmon import cfg
from salmon.checks import mqa_test
from salmon.checks.integrity import (
    check_integrity,
    format_integrity,
    sanitize_integrity,
)
from salmon.checks.logs import check_log_cambia
from salmon.checks.upconverts import upload_upconvert_test
from salmon.common import commandgroup
from salmon.constants import ENCODINGS, FORMATS, SOURCES, TAG_ENCODINGS
from salmon.converter.downconverting import (
    convert_folder,
    generate_conversion_description,
)
from salmon.converter.transcoding import (
    generate_transcode_description,
    transcode_folder,
)
from salmon.errors import AbortAndDeleteFolder, CRCMismatchError, EditedLogError, InvalidMetadataError
from salmon.images import upload_cover
from salmon.tagger import (
    metadata_validator_base,
    validate_encoding,
    validate_source,
)
from salmon.tagger.audio_info import (
    check_hybrid,
    gather_audio_info,
    recompress_path,
)
from salmon.tagger.cover import compress_pictures, download_cover_if_nonexistent
from salmon.tagger.foldername import rename_folder
from salmon.tagger.folderstructure import check_folder_structure
from salmon.tagger.metadata import get_metadata
from salmon.tagger.pre_data import construct_rls_data
from salmon.tagger.retagger import rename_files, tag_files
from salmon.tagger.review import review_metadata
from salmon.tagger.tags import check_tags, gather_tags, standardize_tags
from salmon.uploader.dupe_checker import (
    check_existing_group,
    dupe_check_recent_torrents,
    generate_dupe_check_searchstrs,
    print_recent_upload_results,
    print_torrents,
)
from salmon.uploader.preassumptions import confirm_group_upload, print_preassumptions
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
from salmon.uploader.upload import (
    concat_track_data,
    prepare_and_upload,
)

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option("--group-id", "-g", default=None, help="Group ID to upload torrent to")
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
@click.option("-yyy", is_flag=True, help="Automatically pick the default answer for prompt")
@click.option(
    "--skip-mqa",
    is_flag=True,
    help="Skip check for MQA marker (on first file only)",
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
async def up(
    path: str,
    group_id: int | None,
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
    yyy: bool,
    skip_mqa: bool,
    skip_log_check: bool,
    skip_integrity_check: bool,
) -> None:
    """Command to upload an album folder to a Gazelle Site."""
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
    if group_id:
        await confirm_group_upload(gazelle_site, group_id, source)
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
    )


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
    """
    path = os.path.abspath(path)
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

    try:
        if not skip_mqa:
            click.secho("Checking for MQA release (first file only)", fg="cyan", bold=True)
            mqa_test(path)
            click.secho("No MQA release detected", fg="green")

        if rls_data["encoding"] == "24bit Lossless" and not skip_up:
            if not cfg.upload.yes_all:
                if click.confirm(
                    click.style("\n24bit detected. Do you want to check whether might be upconverted?", fg="magenta"),
                    default=True,
                ):
                    upload_upconvert_test(path)
            else:
                upload_upconvert_test(path)

        if source == "CD" and not skip_log_check:
            click.secho("\nChecking logs", fg="green")
            for root, _, files in os.walk(path):
                for f in files:
                    if f.lower().endswith(".log"):
                        filepath = os.path.join(root, f)
                        click.secho(f"\nScoring {filepath}...", fg="cyan", bold=True)
                        try:
                            check_log_cambia(filepath, path)
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
                        except Exception as e:
                            click.secho(f"Error checking log: {e}", fg="red")

        if group_id is None:
            searchstrs = generate_dupe_check_searchstrs(rls_data["artists"], rls_data["title"], rls_data["catno"])
            if len(searchstrs) > 0:
                group_id = await check_existing_group(gazelle_site, searchstrs)

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
            path, tags, metadata, source, rls_data, recompress, auto_rename, spectral_ids, skip_integrity_check
        )

        if not group_id:
            group_id = await recheck_dupe(gazelle_site, searchstrs, metadata)
            click.echo()
        track_data = concat_track_data(tags, audio_info)
    except click.Abort:
        return click.secho("\nAborting upload...", fg="red")
    except AbortAndDeleteFolder:
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
        await last_min_dupe_check(gazelle_site, searchstrs)

    # Shallow copy to avoid errors on multiple uploads in one session.
    remaining_gazelle_sites = list(salmon.trackers.tracker_list)
    tracker = gazelle_site.site_code
    torrent_id = None
    cover_url = None
    stored_cover_url = None  # Store the cover URL for reuse across trackers
    # Regenerate searchstrs (will be used to search for requests)
    searchstrs = generate_dupe_check_searchstrs(rls_data["artists"], rls_data["title"], rls_data["catno"])

    seedbox_uploader = UploadManager()

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
            searchstrs = generate_dupe_check_searchstrs(rls_data["artists"], rls_data["title"], rls_data["catno"])
            group_id = await check_existing_group(gazelle_site, searchstrs, metadata)

        remaining_gazelle_sites.remove(tracker)

        # Handle cover image for this tracker
        if group_id:
            if not remove_downloaded_cover_image:
                await download_cover_if_nonexistent(path, metadata["cover"])
            # Don't need cover URL for existing groups
            cover_url = None
        else:
            # For new groups, we need a cover URL
            # If we already uploaded it for a previous tracker, reuse that URL
            if not stored_cover_url:
                cover_path, is_downloaded = await download_cover_if_nonexistent(path, metadata["cover"])
                stored_cover_url = await upload_cover(cover_path)
                if is_downloaded and remove_downloaded_cover_image and cover_path:
                    click.secho("Removing downloaded Cover Image File", fg="yellow")
                    os.remove(cover_path)
            cover_url = stored_cover_url

        if not scene and cfg.image.auto_compress_cover:
            compress_pictures(path)

        if not request_id and cfg.upload.requests.check_requests:
            request_id = await check_requests(gazelle_site, searchstrs)

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

        torrent_content.comment = url
        torrent_content.write(torrent_path, overwrite=True)

        await print_torrents(gazelle_site, group_id, highlight_torrent_id=torrent_id)

        if cfg.upload.yes_all or click.confirm(
            click.style("\nWould you like to check downconversion options?", fg="magenta"),
            default=True,
        ):
            selected_tasks = await prompt_downconversion_choice(rls_data, track_data)
            if selected_tasks:
                display_names = [task["name"] for task in selected_tasks]
                click.secho(f"\nSelected formats for downconversion: {', '.join(display_names)}", fg="green", bold=True)

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

        tracker = None
        if not remaining_gazelle_sites or not cfg.upload.multi_tracker_upload:
            click.secho("\nDone uploading this release.", fg="green")
            break

    await seedbox_uploader.execute_upload()


async def edit_metadata(
    path, tags, metadata, source, rls_data, recompress, auto_rename, spectral_ids, skip_integrity_check=False
):
    """
    The metadata editing portion of the uploading process. This sticks the user
    into an infinite loop where the metadata process is repeated until the user
    decides it is ready for upload.
    """
    while True:
        metadata = await review_metadata(metadata, metadata_validator)
        if not metadata["scene"]:
            tag_files(path, tags, metadata, auto_rename)

        tags = check_tags(path)
        if not metadata["scene"] and recompress:
            recompress_path(path)
        path = rename_folder(path, metadata, auto_rename)
        if not metadata["scene"]:
            rename_files(path, tags, metadata, auto_rename, spectral_ids, source)
        await check_folder_structure(path, metadata["scene"])

        if not skip_integrity_check:
            click.secho("\nChecking integrity of audio files...", fg="cyan", bold=True)
            result = check_integrity(path)
            click.echo(format_integrity(result))

            if not result[0] and metadata["scene"]:
                click.secho(
                    "Some files failed sanitization, and this a scene release. "
                    "You need to sanitize and de-scene before uploading. Aborting.",
                    fg="red",
                    bold=True,
                )
                raise click.Abort()
            if not result[0] and (
                cfg.upload.yes_all
                or click.confirm(
                    click.style("\nDo you want to sanitize this upload?", fg="magenta"),
                    default=True,
                )
            ):
                click.secho("\nSanitizing files...", fg="cyan", bold=True)
                if sanitize_integrity(path):
                    click.secho("Sanitization complete", fg="green")
                else:
                    click.secho("Some files failed sanitization", fg="red", bold=True)

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
        return await check_existing_group(gazelle_site, new_searchstrs)
    return None


async def last_min_dupe_check(gazelle_site, searchstrs):
    """Check for dupes in the log one last time before upload.

    Helpful if you are uploading something in race like conditions.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings for dupe checking.
    """
    # Should really avoid asking if already shown the same releases from the log.
    click.secho(f"Last Minute Dupe Check on {gazelle_site.site_code}", fg="cyan")
    recent_uploads = await dupe_check_recent_torrents(gazelle_site, searchstrs)
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


async def prompt_downconversion_choice(rls_data, track_data):
    """
    Prompt user to select downconversion formats.
    Returns a list of selected task dictionaries.
    """
    options = get_downconversion_options(rls_data, track_data)

    if not options:
        return []

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

    selected_tasks = []

    while True:
        try:
            choices = await click.prompt(
                click.style(
                    '\nSelect formats to convert (space-separated list of IDs, "0" for none, "*" for all)', fg="magenta"
                ),
                default="*",
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
    seedbox_uploader: Any,
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
            sample_rate, new_path = convert_folder(
                base_path, bit_depth=task["target_bitdepth"], sample_rate=task["target_sample_rate"]
            )
            time.sleep(0.1)

            # Update metadata for this conversion
            conversion_metadata = metadata.copy()
            if task["target_bitdepth"] == 16:
                conversion_metadata["encoding"] = "Lossless"

            # Generate description for conversion
            description = generate_conversion_description(base_url, sample_rate)
            click.secho(f"  Generated description: {description[:100]}...", fg="blue")
            await check_folder_structure(new_path, conversion_metadata["scene"])

            # Upload the converted version
            torrent_id, group_id, torrent_path, torrent_content, new_url = await upload_and_report(
                gazelle_site,
                new_path,
                group_id,
                conversion_metadata,
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

            click.secho(f"  ✓ {task['name']} conversion completed", fg="green")

        elif task["action"] == "transcode":
            # Call transcode function
            click.secho(f"  Target encoding: {task['encoding']}", fg="white")

            # Execute transcoding
            transcoded_path = transcode_folder(base_path, task["encoding"])
            time.sleep(0.1)

            # Update metadata for this transcode
            transcode_metadata = metadata.copy()
            transcode_metadata["format"] = "MP3"
            transcode_metadata["encoding"] = {"320": "320", "V0": "V0 (VBR)"}[task["encoding"]]
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
    seedbox_uploader: Any,
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
        seedbox_uploader.add_upload_task(path, task_type="folder", is_flac=is_flac)
        seedbox_uploader.add_upload_task(torrent_path, task_type="seed", is_flac=is_flac)

    return torrent_id, group_id, torrent_path, torrent_content, url


def convert_genres(genres):
    """Convert the weirdly spaced genres to RED-compliant genres."""
    return ",".join(re.sub("[-_ ]", ".", g).strip() for g in genres)


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
