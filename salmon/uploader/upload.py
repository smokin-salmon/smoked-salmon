import os
import re
from typing import TYPE_CHECKING, Any

import anyio
import asyncclick as click
from aiohttp import FormData
from torf import Torrent

from salmon import cfg
from salmon.common import str_to_int_if_int
from salmon.constants import ARTIST_IMPORTANCES
from salmon.errors import RequestError
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

    Returns:
        Tuple of (torrent_id, group_id, torrent_path, torrent_content).

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
        )
    torrent_path, torrent_content = generate_torrent(gazelle_site, path)
    files = await compile_files(path, torrent_path, metadata)

    click.secho("Uploading torrent...", fg="yellow")
    try:
        torrent_id, group_id = await gazelle_site.upload(data, files)
        # Ensure group_id is int (upload returns tuple[int, int])
        return torrent_id, int(group_id) if group_id else 0, torrent_path, torrent_content
    except RequestError as e:
        click.secho(str(e), fg="red", bold=True)
        raise SystemExit(1) from e


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

    Returns:
        Data dict for upload POST.
    """
    return {
        "submit": True,
        "type": 0,
        "title": metadata["title"],
        "artists[]": [a[0] for a in metadata["artists"]],
        "importance[]": [ARTIST_IMPORTANCES[a[1]] for a in metadata["artists"]],
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
        "tags": metadata["tags"],
        "image": cover_url,
        "album_desc": generate_description(track_data, metadata),
        "release_desc": generate_t_description(
            metadata, track_data, hybrid, metadata["urls"], spectral_urls, spectral_ids, lossy_comment, source_url
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

    Returns:
        Data dict for upload POST.
    """
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
        "release_desc": override_description
        if override_description
        else generate_t_description(
            metadata, track_data, hybrid, metadata["urls"], spectral_urls, spectral_ids, lossy_comment, source_url
        ),
        "requestid": request_id,
    }


async def compile_files(path: str, torrent_path: str, metadata: dict[str, Any]) -> FormData:
    """Compile files to upload (torrent and log files).

    Args:
        path: Path to the album folder.
        torrent_path: Path to the torrent file.
        metadata: Release metadata.

    Returns:
        FormData containing files to upload.
    """
    files = FormData()
    async with await anyio.open_file(torrent_path, "rb") as torrent_file:
        files.add_field(
            "file_input",
            await torrent_file.read(),
            filename="meowmeow.torrent",
            content_type="application/octet-stream",
        )
    if metadata["source"] == "CD":
        await attach_logfiles(path, files)
    return files


async def attach_logfiles(path: str, files: FormData) -> None:
    """Attach all log files for upload.

    Args:
        path: Path to the album folder.
        files: FormData to add log files to.
    """
    for root, _, filenames in os.walk(path):
        for filename in filenames:
            if filename.lower().endswith(".log"):
                filepath = os.path.abspath(os.path.join(root, filename))
                async with await anyio.open_file(filepath, "rb") as f:
                    files.add_field(
                        "logfiles[]", await f.read(), filename=filename, content_type="application/octet-stream"
                    )


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
    tpath = os.path.join(
        gazelle_site.dot_torrents_dir,
        f"{os.path.basename(path)} - {gazelle_site.site_string}.torrent",
    )
    t.write(tpath, overwrite=True)
    click.secho(" done!", fg="yellow")
    return tpath, t


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

        description += f"{', '.join(track['t'].artist)} - {track['t'].title} [i]({length})[/i]\n"

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

    Returns:
        BBCode description string.
    """
    description = ""
    if spectral_urls:
        description += make_spectral_bbcode(spectral_ids, spectral_urls)

    if not hybrid:
        track = next(iter(track_data.values()))
        if track["precision"]:
            if cfg.upload.description.icons_in_descriptions:
                description += "[img]https://ptpimg.me/pu93q2.png[/img]"
            else:
                description += "Encode Specifics:"
            description += " [b]{} bit [color=#2E86C1]{:.01f}[/color] kHz[/b]".format(
                track["precision"], track["sample rate"] / 1000
            )
            description += "\n"
        else:
            description += "Encode Specifics: {:.01f} kHz\n".format(track["sample rate"] / 1000)

    if metadata["date"]:
        description += f"Released on [b]{metadata['date']}[/b]\n"

    if cfg.upload.description.include_tracklist_in_t_desc or hybrid:
        for filename, track in track_data.items():
            description += os.path.splitext(filename)[0]
            description += " [i]({})[/i]".format(f"{track['duration'] // 60}:{track['duration'] % 60:02d}")
            if cfg.upload.description.bitrates_in_t_desc:
                description += " [{:.01f}kbps]".format(track["bit rate"] / 1000)

            if hybrid:
                description += " [{} bit / {} kHz]".format(track["precision"], track["sample rate"] / 1000)

            description += "\n"
        description += "\n"

    if lossy_comment and cfg.upload.compression.lma_comment_in_t_desc:
        description += f"[u]Lossy Notes:[/u]\n{lossy_comment}\n\n"

    if source_url is not None:
        matched = False
        for name, source in METASOURCES.items():
            if source.Scraper.regex.match(source_url):
                if cfg.upload.description.icons_in_descriptions:
                    description += (
                        f"[b]Source:[/b] [pad=0|3][url={source_url}][img]"
                        f"{SOURCE_ICONS[name]}[/img] {name}[/url][/pad]\n\n"
                    )
                else:
                    description += f"[b]Source:[/b] [url={source_url}]{name}[/url]\n\n"
                matched = True
                break

        if not matched:
            hostname = re.match(r"https?://(?:www\.)?([^/]+)", source_url)
            if hostname:
                description += f"[b]Source:[/b] [url={source_url}]{hostname.group(1)}[/url]\n\n"

    if metadata_urls:
        description += "[b]More info:[/b] " + generate_source_links(metadata_urls, source_url)
        description += "\n"

    return description


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

    for url in metadata_urls:
        matched = False
        for name, source in METASOURCES.items():
            if source.Scraper.regex.match(url):
                if cfg.upload.description.icons_in_descriptions:
                    links.append(f"[pad=0|3][url={url}][img]{SOURCE_ICONS[name]}[/img] {name}[/url][/pad]")
                else:
                    links.append(f"[url={url}]{name}[/url]")
                matched = True
                break

        if not matched:
            hostname = re.match(r"https?://(?:www\.)?([^/]+)", url)
            if hostname:
                unmatched_urls.append(f"[url={url}]{hostname.group(1)}[/url]")

    result = " ".join(links) if cfg.upload.description.icons_in_descriptions else " | ".join(links)

    if unmatched_urls:
        if links:
            result += " | "
        result += " | ".join(unmatched_urls)

    return result
