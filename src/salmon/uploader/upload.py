import os
import re
import unicodedata
from typing import TYPE_CHECKING, Any, Literal

import anyio
import asyncclick as click
from torf import Torrent

from salmon import cfg, dryrun
from salmon.common import UploadFiles, str_to_int_if_int
from salmon.constants import ARTIST_IMPORTANCES
from salmon.release_notification import get_version
from salmon.sources import SOURCE_ICONS
from salmon.tagger.sources import METASOURCES
from salmon.uploader.spectrals import (
    make_spectral_bbcode,
)

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
    torrent_id, group_id = await gazelle_site.upload(data, files)
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

    Args:
        path: Path to the album folder.

    Returns:
        List of (filename, file_bytes) tuples for each log file found.
    """
    log_files: list[tuple[str, bytes]] = []
    async for filepath in anyio.Path(path).rglob("*.log"):
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
    is responsible for writing the torrent with validate=False and reloading it from disk
    afterwards, since the normalized names no longer match the on-disk file names that
    Torrent.validate() checks against.

    Args:
        t: The generated torrent, still pointing at the source folder on disk.
        form: "NFC" or "NFD".
    """
    info = t.metainfo["info"]
    info["name"] = unicodedata.normalize(form, info["name"])
    if "files" in info:
        for fileinfo in info["files"]:
            fileinfo["path"] = [unicodedata.normalize(form, part) for part in fileinfo["path"]]


def generate_torrent(gazelle_site: "BaseGazelleApi", path: str) -> tuple[str, Torrent]:
    """Generate torrent file for the album.

    Args:
        gazelle_site: The tracker API instance.
        path: Path to the album folder.

    Returns:
        Tuple of (torrent_path, torrent_object).
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

    normalization = cfg.upload.torrent_name_normalization
    if normalization in ("", "none"):
        t.write(tpath, overwrite=True)
    else:
        form: Literal["NFC", "NFD"] = "NFC" if normalization == "NFC" else "NFD"
        _normalize_torrent_names(t, form)
        # The normalized names no longer match the file names on disk, so validation
        # against the source folder would fail; skip it, then reload from the written
        # file so later use (dump, write, infohash) does not re-validate against disk.
        t.write(tpath, overwrite=True, validate=False)
        t = Torrent.read(tpath)

    click.secho(" done!", fg="yellow")
    return tpath, t


def format_tracklist_artists(artists: list[str]) -> str:
    """Format a track's artist list for the group description's tracklist.

    Args:
        artists: Track artist names.

    Returns:
        The joined artist names, wrapped in [artist][/artist] BBCode when the
        `artist_tags_in_tracklist` setting is on. A name containing "[" or "]" is
        left plain, since wrapping it could break the tag.
    """
    if not cfg.upload.description.artist_tags_in_tracklist:
        return ", ".join(artists)
    return ", ".join(
        f"[artist]{artist}[/artist]" if "[" not in artist and "]" not in artist else artist for artist in artists
    )


def generate_description(track_data: dict[str, Any], metadata: dict[str, Any]) -> str:
    """Generate group description with tracklist.

    Args:
        track_data: Track information.
        metadata: Release metadata.

    Returns:
        BBCode description string.
    """
    description = "[b][size=4]Tracklist[/size][/b]\n"
    multi_disc = any(
        (
            t["t"].discnumber
            and t["t"].discnumber != "1/1"
            and (t["t"].discnumber.startswith("1/") or int(t["t"].discnumber) > 1)
        )
        for t in track_data.values()
    )
    total_duration = 0
    for track in track_data.values():
        length = "{}:{:02d}".format(track["duration"] // 60, track["duration"] % 60)
        total_duration += track["duration"]
        if multi_disc:
            description += (
                f"[b]{str_to_int_if_int(track['t'].discnumber, zpad=True)}-"
                f"{str_to_int_if_int(track['t'].tracknumber, zpad=True)}.[/b] "
            )
        else:
            description += f"[b]{str_to_int_if_int(track['t'].tracknumber, zpad=True)}.[/b] "

        description += f"{format_tracklist_artists(track['t'].artist)} - {track['t'].title} [i]({length})[/i]\n"

    if len(track_data.values()) > 1:
        description += f"\n[b]Total length: [/b]{total_duration // 60}:{total_duration % 60:02d}\n"

    if metadata["comment"]:
        description += f"\n{metadata['comment']}\n"

    if metadata["urls"]:
        description += "\n[b]More info:[/b] " + generate_source_links(metadata["urls"])

    return description


def generate_t_description(
    metadata: dict[str, Any],
    track_data: dict[str, Any],
    hybrid: bool,
    metadata_urls: list[str],
    spectral_urls: dict[int, list[str]] | None,
    spectral_ids: dict[int, str] | None,
    lossy_comment: str | None,
    source_url: str | None,
    conversion_note: str | None = None,
) -> str:
    """Generate torrent description with spectrals and file info.

    Args:
        metadata: Release metadata.
        track_data: Track information.
        hybrid: Whether this is a hybrid release.
        metadata_urls: Metadata source URLs.
        spectral_urls: Spectral image URLs.
        spectral_ids: Spectral IDs.
        lossy_comment: Lossy approval comment.
        source_url: Source URL.
        conversion_note: How the folder was converted, put before the footer.

    Returns:
        BBCode description string.
    """
    spectrals = make_spectral_bbcode(spectral_ids, spectral_urls) if spectral_urls else ""

    if not hybrid:
        track = next(iter(track_data.values()))
        sample_rate = track["sample rate"] / 1000
        if track["precision"]:
            icon_url = (
                "https://i.ibb.co/hPnL0dT/cdq.png"
                if track["precision"] == 16
                else "https://i.ibb.co/4R16Ktwx/hires.png"
            )
            prefix = f"[img]{icon_url}[/img]" if cfg.upload.description.icons_in_descriptions else "Encode Specifics:"
            encode_specifics = (
                f"{prefix} [b]{track['precision']} bit [color=#2E86C1]{sample_rate:.01f}[/color] kHz[/b]\n"
            )
        else:
            encode_specifics = f"Encode Specifics: {sample_rate:.01f} kHz\n"
    else:
        encode_specifics = ""

    release_date = f"Released on [b]{metadata['date']}[/b]\n" if metadata["date"] else ""

    tracklist = ""
    if cfg.upload.description.include_tracklist_in_t_desc or hybrid:
        for filename, track in track_data.items():
            mins, secs = track["duration"] // 60, track["duration"] % 60
            bitrate = f" [{track['bit rate'] / 1000:.01f}kbps]" if cfg.upload.description.bitrates_in_t_desc else ""
            hybrid_info = f" [{track['precision']} bit / {track['sample rate'] / 1000} kHz]" if hybrid else ""
            tracklist += f"{os.path.splitext(filename)[0]} [i]({mins}:{secs:02d})[/i]{bitrate}{hybrid_info}\n"
        tracklist += "\n"

    lossy_notes = (
        f"[u]Lossy Notes:[/u]\n{lossy_comment}\n\n"
        if lossy_comment and cfg.upload.compression.lma_comment_in_t_desc
        else ""
    )

    source = ""
    if source_url is not None:
        for name, src in METASOURCES.items():
            if src.Scraper.regex.match(source_url):
                source = (
                    f"[b]Source:[/b] [pad=0|3][url={source_url}][img]{SOURCE_ICONS[name]}[/img] {name}[/url][/pad]\n"
                    if cfg.upload.description.icons_in_descriptions
                    else f"[b]Source:[/b] [url={source_url}]{name}[/url]\n"
                )
                break
        if not source:
            hostname = re.match(r"https?://(?:www\.)?([^/]+)", source_url)
            if hostname:
                source = f"[b]Source:[/b] [url={source_url}]{hostname.group(1)}[/url]\n"

    more_info_links = generate_source_links(metadata_urls, source_url) if metadata_urls else ""
    more_info = f"[b]More info:[/b] {more_info_links}\n" if more_info_links else ""

    footer = (
        f"[hr]Uploaded with [url=https://github.com/smokin-salmon/smoked-salmon]"
        f"[b]smoked-salmon[/b] v{get_version()}[/url]"
    )

    conversion = conversion_note or ""
    return f"{spectrals}{encode_specifics}{release_date}{tracklist}{lossy_notes}{source}{more_info}{conversion}{footer}"


def generate_source_links(metadata_urls: list[str], source_url: str | None = None) -> str:
    """Generate BBCode links for metadata sources.

    Args:
        metadata_urls: List of metadata source URLs.
        source_url: Optional source URL to exclude.

    Returns:
        BBCode formatted links string.
    """
    links: list[str] = []
    unmatched_urls: list[str] = []
    excluded_urls = {source_url.strip()} if source_url else set()

    for url in metadata_urls:
        normalized_url = url.strip()
        if normalized_url in excluded_urls:
            continue

        matched = False
        for name, source in METASOURCES.items():
            if source.Scraper.regex.match(normalized_url):
                if cfg.upload.description.icons_in_descriptions:
                    links.append(f"[pad=0|3][url={normalized_url}][img]{SOURCE_ICONS[name]}[/img] {name}[/url][/pad]")
                else:
                    links.append(f"[url={normalized_url}]{name}[/url]")
                matched = True
                break

        if not matched:
            hostname = re.match(r"https?://(?:www\.)?([^/]+)", normalized_url)
            if hostname:
                unmatched_urls.append(f"[url={normalized_url}]{hostname.group(1)}[/url]")

    result = " ".join(links) if cfg.upload.description.icons_in_descriptions else " | ".join(links)

    if unmatched_urls:
        if links:
            result += " | "
        result += " | ".join(unmatched_urls)

    return result
