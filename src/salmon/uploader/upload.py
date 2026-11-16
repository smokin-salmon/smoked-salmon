import os
import shutil
import tempfile
import unicodedata
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal

import anyio
import asyncclick as click
from torf import TorfError, Torrent

from salmon import cfg, dryrun
from salmon.common import UploadFiles, clipboard_copy
from salmon.common.files import is_apple_double
from salmon.constants import ARTIST_IMPORTANCES
from salmon.errors import RateLimitedError, TLSCertificateError, UploadRefusedError
from salmon.trackers.base import RetryableError
from salmon.uploader import record
from salmon.uploader.description import generate_description, generate_t_description
from salmon.uploader.seedbox import UploadManager, warn_unmatched_normalization
from salmon.uploader.spectrals import report_lossy_master

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


async def prepare_and_upload(
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
    source_url: str | None = None,
    override_description: str | None = None,
    conversion_note: str | None = None,
) -> tuple[int, int, str, Torrent]:
    """Compile data and upload torrent to tracker.

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
        override_description: Override torrent description.
        conversion_note: How the folder was converted, added to the generated description.

    Returns:
        Tuple of (torrent_id, group_id, torrent_path, torrent_content). In a dry run, nothing is uploaded: the
        IDs are dryrun.NEW_TORRENT_ID and the group's (dryrun.NEW_GROUP_ID for a new one), and the torrent
        file is in the dry run's scratch directory.

    Raises:
        SystemExit: If upload fails.
    """
    if not group_id:
        data = compile_data_new_group(
            gazelle_site,
            path,
            metadata,
            track_data,
            hybrid,
            cover_url,
            spectral_urls,
            spectral_ids,
            lossy_comment,
            request_id,
            source_url=source_url,
            override_description=override_description,
            conversion_note=conversion_note,
        )
    else:
        data = compile_data_existing_group(
            gazelle_site,
            path,
            group_id,
            metadata,
            track_data,
            hybrid,
            spectral_urls,
            spectral_ids,
            lossy_comment,
            request_id,
            source_url=source_url,
            override_description=override_description,
            conversion_note=conversion_note,
        )
    await gazelle_site.ensure_authenticated()
    torrent_path, torrent_content = generate_torrent(gazelle_site, path)
    files = await compile_files(path, torrent_content, metadata)

    if not dryrun.active():
        click.secho("Uploading torrent...", fg="yellow")
    # Counts as sent unless it can't have been, so a bug counts. So does a GET that fails first
    # (RedApi.site_page_upload): that only costs a person a look.
    sent = not dryrun.active()
    try:
        torrent_id, group_id = await gazelle_site.upload(data, files)
    except (click.Abort, RetryableError, RateLimitedError, TLSCertificateError):
        sent = False  # A question before the POST (OPS's Split, DIC's marks), or the tracker can't have acted on it.
        raise
    finally:
        if sent:
            record.note_sent()
    if dryrun.active():
        # The group may be dryrun.NEW_GROUP_ID, which int() would turn into a plain -1.
        return torrent_id, group_id, torrent_path, torrent_content
    # Ensure group_id is int (upload returns tuple[int, int])
    return torrent_id, int(group_id) if group_id else 0, torrent_path, torrent_content


def concat_track_data(tags: dict[str, Any], audio_info: dict[str, Any]) -> dict[str, Any]:
    """Combine the tag and audio data into one dictionary per track.

    Args:
        tags: Tag data keyed by filename.
        audio_info: Audio info keyed by filename.

    Returns:
        Combined track data dict.
    """
    track_data: dict[str, Any] = {}
    for k, v in audio_info.items():
        track_data[k] = {**v, "t": tags[k]}
    return track_data


def _filter_unsupported_artists(
    gazelle_site: "BaseGazelleApi", artists: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Drop artists whose role this tracker's upload form does not offer.

    Prints one warning line per dropped role, naming the artists it drops.

    Args:
        gazelle_site: The tracker API instance.
        artists: List of (artist_name, role) tuples.

    Returns:
        The artists list with unsupported roles removed.
    """
    unsupported = gazelle_site.unsupported_artist_roles
    if not unsupported:
        return artists

    dropped_by_role: dict[str, list[str]] = {}
    kept: list[tuple[str, str]] = []
    for name, role in artists:
        if role in unsupported:
            dropped_by_role.setdefault(role, []).append(name)
        else:
            kept.append((name, role))

    for role, names in dropped_by_role.items():
        click.secho(
            f"{gazelle_site.site_string} has no {role.title()} role: not crediting {', '.join(names)}",
            fg="yellow",
        )

    return kept


def compile_data_new_group(
    gazelle_site: "BaseGazelleApi",
    path: str,
    metadata: dict[str, Any],
    track_data: dict[str, Any],
    hybrid: bool,
    cover_url: str | None,
    spectral_urls: dict[int, list[str]] | None,
    spectral_ids: dict[int, str] | None,
    lossy_comment: str | None,
    request_id: int | str | None = None,
    source_url: str | None = None,
    override_description: str | None = None,
    conversion_note: str | None = None,
) -> dict[str, Any]:
    """Compile data for a new torrent group upload.

    Args:
        gazelle_site: The tracker API instance.
        path: Path to the album folder.
        metadata: Release metadata.
        track_data: Track information.
        hybrid: Whether this is a hybrid release.
        cover_url: Cover image URL.
        spectral_urls: Spectral image URLs.
        spectral_ids: Spectral IDs.
        lossy_comment: Lossy approval comment.
        request_id: Request ID to fill.
        source_url: Source URL.
        override_description: Override torrent description.
        conversion_note: How the folder was converted, added to the generated description.

    Returns:
        Data dict for upload POST.
    """
    tracker_fields = gazelle_site.upload_form_fields(metadata, track_data)
    artists = _filter_unsupported_artists(gazelle_site, metadata["artists"])
    return {
        "submit": True,
        "type": 0,
        "title": metadata["title"],
        "artists[]": [a[0] for a in artists],
        "importance[]": [ARTIST_IMPORTANCES[a[1]] for a in artists],
        "year": metadata["group_year"],
        "record_label": metadata["label"],
        "catalogue_number": generate_catno(metadata),
        "releasetype": gazelle_site.release_types[metadata["rls_type"]],
        "remaster": True,
        "remaster_year": metadata["year"],
        "remaster_title": metadata["edition_title"],
        "remaster_record_label": metadata["label"],
        "remaster_catalogue_number": generate_catno(metadata),
        "format": metadata["format"],
        "bitrate": metadata["encoding"],
        "other_bitrate": None,
        **({"scene": metadata["scene"]} if metadata.get("scene") else {}),
        "vbr": metadata["encoding_vbr"],
        "media": metadata["source"],
        **tracker_fields,
        "tags": metadata["tags"],
        "image": cover_url,
        "album_desc": generate_description(track_data, metadata),
        "release_desc": override_description
        if override_description
        else generate_t_description(
            metadata,
            track_data,
            hybrid,
            metadata["urls"],
            spectral_urls,
            spectral_ids,
            lossy_comment,
            source_url,
            conversion_note,
        ),
        "requestid": request_id,
    }


def compile_data_existing_group(
    gazelle_site: "BaseGazelleApi",
    path: str,
    group_id: int,
    metadata: dict[str, Any],
    track_data: dict[str, Any],
    hybrid: bool,
    spectral_urls: dict[int, list[str]] | None,
    spectral_ids: dict[int, str] | None,
    lossy_comment: str | None,
    request_id: int | str | None,
    source_url: str | None = None,
    override_description: str | None = None,
    conversion_note: str | None = None,
) -> dict[str, Any]:
    """Compile data for upload to an existing group.

    Args:
        gazelle_site: The tracker API instance.
        path: Path to the album folder.
        group_id: Existing group ID.
        metadata: Release metadata.
        track_data: Track information.
        hybrid: Whether this is a hybrid release.
        spectral_urls: Spectral image URLs.
        spectral_ids: Spectral IDs.
        lossy_comment: Lossy approval comment.
        request_id: Request ID to fill.
        source_url: Source URL.
        override_description: Override torrent description.
        conversion_note: How the folder was converted, added to the generated description.

    Returns:
        Data dict for upload POST.
    """
    tracker_fields = gazelle_site.upload_form_fields(metadata, track_data)
    return {
        "submit": True,
        "type": 0,
        "groupid": group_id,
        "remaster": True,
        "remaster_year": metadata["year"],
        "remaster_title": metadata["edition_title"],
        "remaster_record_label": metadata["label"],
        "remaster_catalogue_number": generate_catno(metadata),
        "format": metadata["format"],
        "bitrate": metadata["encoding"],
        **({"scene": metadata["scene"]} if metadata.get("scene") else {}),
        "other_bitrate": None,
        "vbr": metadata["encoding_vbr"],
        "media": metadata["source"],
        **tracker_fields,
        "release_desc": override_description
        if override_description
        else generate_t_description(
            metadata,
            track_data,
            hybrid,
            metadata["urls"],
            spectral_urls,
            spectral_ids,
            lossy_comment,
            source_url,
            conversion_note,
        ),
        "requestid": request_id,
    }


async def compile_files(path: str, torrent_content: Torrent, metadata: dict[str, Any]) -> UploadFiles:
    """Compile files to upload (torrent and log files).

    Args:
        path: Path to the album folder.
        torrent_content: Torrent object.
        metadata: Release metadata.

    Returns:
        UploadFiles containing file data for upload.
    """
    torrent_data = torrent_content.dump()
    log_files = await collect_logfiles(path) if metadata["source"] == "CD" else []
    return UploadFiles(torrent_data=torrent_data, log_files=log_files)


async def collect_logfiles(path: str) -> list[tuple[str, bytes]]:
    """Collect all log files for upload.

    A log is any file ending ".log" in any case ("Rip.LOG" too), as the log checks find them.

    Args:
        path: Path to the album folder.

    Returns:
        List of (filename, file_bytes) tuples for each log file found.
    """
    log_files: list[tuple[str, bytes]] = []
    # Not rglob("*.log"), which matches case-sensitively; case_sensitive=False needs Python 3.12.
    async for filepath in anyio.Path(path).rglob("*"):
        # The name, not .suffix: a file named ".log" has no suffix.
        if filepath.name.lower().endswith(".log") and not is_apple_double(filepath.name) and await filepath.is_file():
            log_files.append((filepath.name, await filepath.read_bytes()))
    return log_files


def generate_catno(metadata: dict[str, Any]) -> str:
    """Generate catalog number from metadata.

    Args:
        metadata: Release metadata.

    Returns:
        Catalog number string.
    """
    if metadata.get("catno"):
        return metadata["catno"]
    elif cfg.upload.compression.use_upc_as_catno:
        return metadata.get("upc", "")
    return ""


def _normalize_torrent_names(t: Torrent, form: Literal["NFC", "NFD"]) -> None:
    """Normalize the Unicode form of every file path component in a torrent's info dict.

    Only the names change: piece hashes cover file contents, so they stay valid. The caller
    reloads the torrent from its own bytes with validate=False, since the normalized names no
    longer match the on-disk file names that Torrent.validate() checks against.

    Args:
        t: The generated torrent, still pointing at the source folder on disk.
        form: "NFC" or "NFD".

    Raises:
        UploadRefusedError: If two files end up with the same path once normalized.
    """
    info = t.metainfo["info"]
    info["name"] = unicodedata.normalize(form, info["name"])
    seen: set[tuple[str, ...]] = set()
    for fileinfo in info.get("files", []):
        fileinfo["path"] = [unicodedata.normalize(form, part) for part in fileinfo["path"]]
        path = tuple(fileinfo["path"])
        if path in seen:
            raise UploadRefusedError(
                f"Two files are both {'/'.join(path)} once {form}-normalized: rename one, "
                'or set torrent_name_normalization to "none".'
            )
        seen.add(path)
    # A file whose path is also another file's folder cannot be laid out either.
    for path in seen:
        for end in range(1, len(path)):
            if path[:end] in seen:
                raise UploadRefusedError(
                    f"A file and a folder are both {'/'.join(path[:end])} once {form}-normalized: rename one, "
                    'or set torrent_name_normalization to "none".'
                )


def generate_torrent(gazelle_site: "BaseGazelleApi", path: str, normalize: bool = True) -> tuple[str, Torrent]:
    """Generate torrent file for the album.

    Args:
        gazelle_site: The tracker API instance.
        path: Path to the album folder.
        normalize: Apply torrent_name_normalization. False names the files exactly as on disk.

    Returns:
        Tuple of (torrent_path, torrent_object).

    Raises:
        UploadRefusedError: If the torrent cannot be written: that tracker's upload fails, before the POST.
    """
    click.secho("Generating torrent file...", fg="yellow", nl=False)
    t = Torrent(
        path,
        trackers=[gazelle_site.announce],
        private=True,
        source=gazelle_site.site_string,
    )
    t.generate()
    # dot_torrents_dir is often a torrent client's watch folder: a dry run writes nothing there.
    tpath = os.path.join(
        dryrun.scratch_dir() if dryrun.active() else gazelle_site.dot_torrents_dir,
        f"{os.path.basename(path)} - {gazelle_site.site_string}.torrent",
    )

    normalization = cfg.upload.torrent_name_normalization if normalize else ""
    if normalization not in ("", "none"):
        form: Literal["NFC", "NFD"] = "NFC" if normalization == "NFC" else "NFD"
        _normalize_torrent_names(t, form)
        warn_unmatched_normalization(form, gazelle_site.site_code)
        # Reloaded in memory, so dumps no longer validate against disk; never from tpath, a watch folder may take it.
        t = Torrent.read_stream(t.dump(validate=False))
    try:
        t.write(tpath, overwrite=True)
    except (OSError, TorfError) as err:
        raise UploadRefusedError(f"Could not write {err}") from err

    click.secho(" done!", fg="yellow")
    return tpath, t


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

    The tracker has the torrent by now: the .torrent rewrite or the clipboard failing says so and does not stop the
    rest, so the torrent is still reported and its seedbox tasks still queued.

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
    _rewrite_torrent(torrent_content, torrent_path)

    # Display success message
    click.secho(
        f"Successfully uploaded {url} ({os.path.basename(path)}).",
        fg="green",
        bold=True,
    )
    record.note_upload(gazelle_site.site_code, format, url)

    # Copy URL to clipboard
    if cfg.upload.description.copy_uploaded_url_to_clipboard:
        clipboard_copy(url)

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


def _rewrite_torrent(torrent_content: "Torrent", torrent_path: str) -> None:
    """Write the torrent over its .torrent file whole, or not at all.

    Written to a new file of its own beside it first, then moved over it: a write that fails part way (a full disk)
    leaves the .torrent the upload was made from, without the URL, which seeds just as well. No other file is written
    or removed, whatever its name.
    """
    folder, name = os.path.split(torrent_path)
    temp = None
    try:
        handle, temp = tempfile.mkstemp(dir=folder or ".", prefix=f"{name}.", suffix=".part")
        os.close(handle)
        # mkstemp's file is readable by its owner only: give it the mode of the .torrent it replaces, which a
        # torrent client running as another user may read.
        with suppress(FileNotFoundError):
            shutil.copymode(torrent_path, temp)
        torrent_content.write(temp, overwrite=True)
        os.replace(temp, torrent_path)
    except (OSError, TorfError) as err:
        if temp is not None:
            with suppress(OSError):
                os.remove(temp)
        click.secho(f"Could not add the URL to {torrent_path} ({err}): it seeds without it.", fg="yellow")
