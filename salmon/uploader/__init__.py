import asyncio
import os
import platform
import re
import shutil

import click
import pyperclip

import salmon.trackers
from salmon import config
from salmon.checks import mqa_test
from salmon.checks.integrity import (
    check_integrity,
    format_integrity,
    sanitize_integrity,
)
from salmon.checks.upconverts import upload_upconvert_test
from salmon.common import commandgroup
from salmon.constants import ENCODINGS, FORMATS, SOURCES, TAG_ENCODINGS
from salmon.errors import AbortAndDeleteFolder, InvalidMetadataError
from salmon.images import upload_cover
from salmon.qbittorrent.qbittorrentapi import add_torrent_to_qbittorrent
from salmon.rutorrent.rutorrent import add_torrent_to_rutorrent
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
from salmon.tagger.cover import download_cover_if_nonexistent
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
)
from salmon.uploader.preassumptions import print_preassumptions
from salmon.uploader.request_checker import check_requests
from salmon.uploader.spectrals import (
    check_spectrals,
    generate_lossy_approval_comment,
    handle_spectrals_upload_and_deletion,
    post_upload_spectral_check,
    report_lossy_master,
)
from salmon.uploader.upload import (
    concat_track_data,
    prepare_and_upload,
)

loop = asyncio.get_event_loop()


@commandgroup.command()
@click.argument(
    "path", type=click.Path(exists=True, file_okay=False, resolve_path=True)
)
@click.option("--group-id", "-g", default=None, help="Group ID to upload torrent to")
@click.option(
    "--source",
    "-s",
    type=click.STRING,
    callback=validate_source,
    help=f'Source of files ({"/".join(SOURCES.values())})',
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
    help=f'Uploading Choices: ({"/".join(salmon.trackers.tracker_list)})',
)
@click.option("--request", "-r", default=None, help='Pass a request URL or ID')
@click.option(
    "--spectrals-after",
    "-a",
    is_flag=True,
    help='Assess / upload / report spectrals after torrent upload',
)
@click.option(
    "--auto-rename",
    "-n",
    is_flag=True,
    help='Rename files and folders automatically',
)
@click.option(
    "--skip-up",
    is_flag=True,
    help='Skip check for 24 bit upconversion',
)
@click.option(
    "--scene",
    is_flag=True,
    help='Is this a scene release (default: False)'
)
@click.option(
    "--rutorrent",
    is_flag=True,
    help='Adds torrent to Rutorrent client after torrent upload (default: False)'
)
@click.option(
    "--qbittorrent",
    is_flag=True,
    default=config.ENABLE_QBITTORRENT_INJECTION,
    help='Adds torrent to qBitTorrent client after torrent upload (default: False)'
)
@click.option("--source-url", "-su", 
    default=None, 
    help='For WEB uploads provide the source of the album to be added in release description'
)
@click.option(
    "-yyy",
    is_flag=True,
    help='Automatically pick the default answer for prompt'
)
@click.option(
    "--skip-mqa",
    is_flag=True,
    help='Skip check for MQA marker (on first file only)',
)
def up(
    path,
    group_id,
    source,
    lossy,
    spectrals,
    overwrite,
    encoding,
    compress,
    tracker,
    request,
    spectrals_after,
    auto_rename,
    skip_up,
    scene,
    rutorrent,
    qbittorrent,
    source_url,
    yyy,
    skip_mqa,
):
    """Command to upload an album folder to a Gazelle Site."""
    if yyy:
        config.YES_ALL = True
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
    if source_url:
        source_url = source_url.strip()
    upload(
        gazelle_site,
        path,
        group_id,
        source,
        lossy,
        spectrals,
        encoding,
        source_url=source_url,
        scene=scene,
        rutorrent=rutorrent,
        qbittorrent=qbittorrent,
        overwrite_meta=overwrite,
        recompress=compress,
        request_id=request,
        spectrals_after=spectrals_after,
        auto_rename=auto_rename,
        skip_up=skip_up,
        skip_mqa=skip_mqa,
    )


def upload(
    gazelle_site,
    path,
    group_id,
    source,
    lossy,
    spectrals,
    encoding,
    scene=False,
    rutorrent=False,
    qbittorrent=False,
    overwrite_meta=False,
    recompress=False,
    source_url=None,
    searchstrs=None,
    request_id=None,
    spectrals_after=False,
    auto_rename=False,
    skip_up=False,
    skip_mqa=False,
):
    """Upload an album folder to Gazelle Site
    Offer the choice to upload to another tracker after completion."""
    path = os.path.abspath(path)
    remove_downloaded_cover_image = scene or config.REMOVE_AUTO_DOWNLOADED_COVER_IMAGE
    if not source:
        source = _prompt_source()
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
            click.secho("Checking for MQA release (first file only)", fg="yellow", bold=True)
            mqa_test(path)
            click.secho("No MQA release detected", fg="green")

        if rls_data["encoding"] == "24bit Lossless" and not skip_up:
            if not config.YES_ALL:
                if click.confirm(
                        click.style(
                            "\n24bit detected. Do you want to check whether might be upconverted?",
                            fg="magenta"),
                        default=True,):
                    upload_upconvert_test(path)
            else:
                upload_upconvert_test(path)

        if group_id is None:
            searchstrs = generate_dupe_check_searchstrs(
                rls_data["artists"], rls_data["title"], rls_data["catno"]
            )
            if len(searchstrs) > 0:
                group_id = check_existing_group(gazelle_site, searchstrs)

        spectral_ids = None
        if spectrals_after:
            lossy_master = False
            # We tell the uploader not to worry about it being lossy until later.
        else:
            lossy_master, spectral_ids = check_spectrals(
                path, audio_info, lossy, spectrals, format=rls_data["format"]
            )

        metadata, new_source_url = get_metadata(path, tags, rls_data)
        if new_source_url is not None:
            source_url = new_source_url
            click.secho(f"New Source URL: {source_url}", fg="yellow")
        path, metadata, tags, audio_info = edit_metadata(
            path, tags, metadata, source, rls_data, recompress, auto_rename, spectral_ids
        )

        if not group_id:
            group_id = recheck_dupe(gazelle_site, searchstrs, metadata)
            click.echo()
        track_data = concat_track_data(tags, audio_info)
    except click.Abort:
        return click.secho("\nAborting upload...", fg="red")
    except AbortAndDeleteFolder:
        if platform.system() == "Windows" and config.WINDOWS_USE_RECYCLE_BIN:
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
            lossy_comment = generate_lossy_approval_comment(
                source_url, list(track_data.keys())
            )
            click.echo()

        spectrals_path = os.path.join(path, "Spectrals")
        spectral_urls = handle_spectrals_upload_and_deletion(
            spectrals_path, spectral_ids
        )
    if config.LAST_MINUTE_DUPE_CHECK:
        last_min_dupe_check(gazelle_site, searchstrs)

    # existing torrent group, only download cover image when it won't be removed
    cover_url = None
    if group_id:
        if not remove_downloaded_cover_image:
            download_cover_if_nonexistent(path, metadata["cover"])
    # new torrent group, download cover image and upload to third party host
    else:
        cover_path, is_downloaded = download_cover_if_nonexistent(path, metadata["cover"])
        cover_url = upload_cover(cover_path)
        if is_downloaded and remove_downloaded_cover_image:
            click.secho("Removing downloaded Cover Image File", fg="yellow")
            os.remove(cover_path)

    # Shallow copy to avoid errors on multiple uploads in one session.
    remaining_gazelle_sites = list(salmon.trackers.tracker_list)
    tracker = gazelle_site.site_code
    torrent_id = None
    # Regenerate searchstrs (will be used to search for requests)
    searchstrs = generate_dupe_check_searchstrs(
                rls_data["artists"], rls_data["title"], rls_data["catno"]
            )

    while True:
        # Loop until we don't want to upload to any more sites.
        if not tracker:
            if spectrals_after and torrent_id:
                # Here we are checking the spectrals after uploading to the first site
                # if they were not done before.
                lossy_master, lossy_comment, spectral_urls, spectral_ids = post_upload_spectral_check(
                    gazelle_site, path, torrent_id, None, track_data, source, source_url, format=rls_data["format"]
                )
                spectrals_after = False
            click.secho(
                "\nWould you like to upload to another tracker? ", fg="magenta", nl=False
            )
            tracker = salmon.trackers.choose_tracker(remaining_gazelle_sites)
            gazelle_site = salmon.trackers.get_class(tracker)()

            click.secho(f"Uploading to {gazelle_site.base_url}", fg="cyan", bold=True)
            searchstrs = generate_dupe_check_searchstrs(
                rls_data["artists"], rls_data["title"], rls_data["catno"]
            )
            group_id = check_existing_group(gazelle_site, searchstrs, metadata)

        remaining_gazelle_sites.remove(tracker)
        if not request_id and config.CHECK_REQUESTS:
            request_id = check_requests(gazelle_site, searchstrs)

        torrent_id, torrent_path, torrent_file = prepare_and_upload(
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
            source_url
        )
        if lossy_master:
            report_lossy_master(
                gazelle_site,
                torrent_id,
                spectral_urls,
                spectral_ids,
                source,
                lossy_comment,
                source_url=source_url,
            )

        url = f"{gazelle_site.base_url}/torrents.php?torrentid={torrent_id}"
        click.secho(
            f"\nSuccessfully uploaded {url} ({os.path.basename(path)}).",
            fg="green",
            bold=True,
        )
        if rutorrent:
            click.secho(
            (f"\nAdding torrent to client {config.RUTORRENT_URL} "
             f"{config.TRACKER_DIRS[tracker]} {config.TRACKER_LABELS[tracker]}"),
            fg="green",
            bold=True
            )
            add_torrent_to_rutorrent(
                config.RUTORRENT_URL,
                torrent_path,
                config.TRACKER_DIRS[tracker],
                config.TRACKER_LABELS[tracker]
            )
        if qbittorrent:
            click.secho(
            (f"\nAdding torrent to client {config.QBITTORRENT_HOST} "
             f"Save Path: {config.DOWNLOAD_DIRECTORY}, Category: {config.QBITTORRENT_CATEGORY}"),
            fg="green",
            bold=True
            )
            qbit_success = add_torrent_to_qbittorrent(
                config.QBITTORRENT_HOST,
                config.QBITTORRENT_PORT,
                config.QBITTORRENT_USERNAME,
                config.QBITTORRENT_PASSWORD,
                torrent_path,
                save_path=config.DOWNLOAD_DIRECTORY,
                category=config.QBITTORRENT_CATEGORY,
                skip_checking=config.QBITTORRENT_SKIP_HASH_CHECK
            )
            # Remove the torrent file after successful qBittorrent upload
            if qbit_success:
                try:
                    os.remove(torrent_path)
                except OSError as e:
                    click.secho(f"Warning: Could not remove torrent file: {e}", fg="yellow")
            else:
                click.secho(
                    f"Warning: Failed to add torrent to qBittorrent. "
                    f"You can manually add the torrent file from: {torrent_path}",
                    fg="yellow"
                )

        if config.COPY_UPLOADED_URL_TO_CLIPBOARD:
            pyperclip.copy(url)
        tracker = None
        request_id = None
        if not remaining_gazelle_sites or not config.MULTI_TRACKER_UPLOAD:
            return click.secho("\nDone uploading this release.", fg="green")


def edit_metadata(path, tags, metadata, source, rls_data, recompress, auto_rename, spectral_ids):
    """
    The metadata editing portion of the uploading process. This sticks the user
    into an infinite loop where the metadata process is repeated until the user
    decides it is ready for upload.
    """
    while True:
        metadata = review_metadata(metadata, metadata_validator)
        if not metadata['scene']:
            tag_files(path, tags, metadata, auto_rename)

        tags = check_tags(path)
        if not metadata['scene'] and recompress:
            recompress_path(path)
        path = rename_folder(path, metadata, auto_rename)
        if not metadata['scene']:
            rename_files(path, tags, metadata, auto_rename, spectral_ids, source)
        check_folder_structure(path, metadata['scene'])

        if config.YES_ALL or click.confirm(
            click.style(
                "\nDo you want to check for integrity of this upload?",
                fg="magenta"),
            default=True,
            ):
            result = check_integrity(path)
            click.echo(format_integrity(result))
            
            if not result[0] and metadata['scene']:
                click.secho(
                    "Some files failed sanitization, and this a scene release. "
                    "You need to sanitize and de-scene before uploading. Aborting.",
                    fg="red",
                    bold=True,
                )
                raise click.Abort()
            if not result[0] and (config.YES_ALL or click.confirm(
                click.style(
                    "\nDo you want to sanitize this upload?",
                    fg="magenta"),
                default=True,
                )):
                click.secho("\nSanitizing files...", fg="cyan", bold=True)
                if sanitize_integrity(path):
                    click.secho("Sanitization complete", fg="green")
                else:
                    click.secho("Some files failed sanitization", fg="red", bold=True)

        if config.YES_ALL or click.confirm(
            click.style(
                "\nWould you like to upload the torrent? (No to re-run metadata "
                "section)",
                fg="magenta"
            ),
            default=True,
        ):
            metadata["tags"] = convert_genres(metadata["genres"])
            break

        # Refresh tags to accomodate differences in file structure.
        tags = gather_tags(path)

    tags = gather_tags(path)
    audio_info = gather_audio_info(path)
    return path, metadata, tags, audio_info


def recheck_dupe(gazelle_site, searchstrs, metadata):
    "Rechecks for a dupe if the artist, album or catno have changed."
    new_searchstrs = generate_dupe_check_searchstrs(
        metadata["artists"], metadata["title"], metadata["catno"]
    )
    if (
        searchstrs
        and any(n not in searchstrs for n in new_searchstrs)
        or not searchstrs
        and new_searchstrs
    ):
        click.secho(
            f'\nRechecking for dupes on {gazelle_site.site_string} '
            'due to metadata changes...',
            fg="cyan",
            bold=True,
            nl=False,
        )
        return check_existing_group(gazelle_site, new_searchstrs)


def last_min_dupe_check(gazelle_site, searchstrs):
    "Check for dupes in the log on last time before upload."
    "Helpful if you are uploading something in race like conditions."

    # Should really avoid asking if already shown the same releases from the log.
    click.secho(f"Last Minuite Dupe Check on {gazelle_site.site_code}", fg="cyan")
    recent_uploads = dupe_check_recent_torrents(gazelle_site, searchstrs)
    if recent_uploads:
        print_recent_upload_results(
            gazelle_site, recent_uploads, " / ".join(searchstrs)
        )
        if not click.confirm(
            click.style("\nWould you still like to upload?", fg="red", bold=True,),
            default=False,
        ):
            raise click.Abort
    else:
        click.secho(f"Nothing found on {gazelle_site.site_code}", fg="green")


def metadata_validator(metadata):
    """Validate that the provided metadata is not an issue."""
    metadata = metadata_validator_base(metadata)
    if metadata["format"] not in FORMATS.values():
        raise InvalidMetadataError(f'{metadata["format"]} is not a valid format.')
    if metadata["encoding"] not in ENCODINGS:
        raise InvalidMetadataError(f'{metadata["encoding"]} is not a valid encoding.')

    return metadata


def convert_genres(genres):
    """Convert the weirdly spaced genres to RED-compliant genres."""
    return ",".join(re.sub("[-_ ]", ".", g).strip() for g in genres)


def _prompt_source():
    click.echo(f'\nValid sources: {", ".join(SOURCES.values())}')
    while True:
        sauce = click.prompt(
            click.style(
                "What is the source of this release? [a]bort", fg="magenta"
            ),
            default="",
        )
        try:
            return SOURCES[sauce.lower()]
        except KeyError:
            if sauce.lower().startswith("a"):
                raise click.Abort from None
            click.secho(f"{sauce} is not a valid source.", fg="red")
