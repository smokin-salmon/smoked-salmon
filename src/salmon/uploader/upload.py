import os
import re
import shutil
import tempfile
import unicodedata
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal

import anyio
import asyncclick as click
from torf import TorfError, Torrent

from salmon import cfg, dryrun
from salmon.common import UploadFiles, clipboard_copy, re_split
from salmon.common.numbers import tag_number, unreadable_number
from salmon.common.strings import names_no_artist, reaches_various_threshold, strip_numbered_suffixes
from salmon.constants import ARTIST_IMPORTANCES
from salmon.errors import UploadRefusedError
from salmon.release_notification import get_version
from salmon.sources import SOURCE_ICONS
from salmon.tagger.combine import _extract_remixers_from_title
from salmon.tagger.pre_data import parse_artists
from salmon.tagger.sources import METASOURCES
from salmon.uploader import record
from salmon.uploader.seedbox import UploadManager, warn_unmatched_normalization
from salmon.uploader.spectrals import (
    make_spectral_bbcode,
    report_lossy_master,
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
        if filepath.name.lower().endswith(".log") and await filepath.is_file():
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
    t.write(tpath, overwrite=True)

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


# Release types whose tracklist names every track's artist: on a compilation, anthology or DJ mix, the artist is the
# point of each line.
_ARTIST_ON_EVERY_LINE = frozenset({"Anthology", "Compilation", "DJ Mix"})


def album_credit_names(metadata: dict[str, Any]) -> frozenset[str] | None:
    """The names a track must credit, and no others, for the tracklist to leave its artist out (#895).

    They are the album's main artists and conductors, as ARTIST leads with them, each split as salmon splits the
    artists it reads from tags ("Simon & Garfunkel" is Simon and Garfunkel), so a credit read from the files and
    one from a store compare alike.

    Returns:
        The names, or None when every line keeps its artist: no album artists are given, the release is a
        compilation, anthology or DJ mix, or its main artists are various (as ALBUMARTIST would name them).
    """
    artists = metadata.get("artists") or []
    mains = [name for name, role in artists if role == "main"]
    if not mains or metadata.get("rls_type") in _ARTIST_ON_EVERY_LINE:
        return None
    if reaches_various_threshold(mains) or any(names_no_artist(name) for name in mains):
        return None
    return _split_names([*mains, *(name for name, role in artists if role == "conductor")])


# A feature or remix credit in a title, in any case: "feat.", "Featuring", "ft.", "Remix" or "remixed" as a whole word,
# or a bracket opening with "with" or "vs." ("Song (with B)").
_CREDIT_IN_TITLE = re.compile(
    r"\b(?:feat|featuring|ft|remix(?:e[ds])?)\b|[(\[]\s*(?:with|vs\.?)\s", flags=re.IGNORECASE
)


def credits_only_the_album(credits: list[tuple[str, str]], title: str, album: frozenset[str]) -> bool:
    """Whether a track credits the album's artists and no one else, so its tracklist line can leave them out: its
    credits, as (name, role), have exactly the album's main artists and conductors (see album_credit_names) and no
    guest or remixer, and its title credits no feature or remix (nor a remixer salmon reads from a title, as in
    "Song (X Mix)"). The title is where salmon puts a remixer (REMIXER only names one the store credits) and, with
    guests_in_track_title, a guest; an AAC file has neither ARTISTS nor REMIXER.

    Its other roles (composer, producer, arranger) never show in the tracklist, so they don't count.
    """
    if any(role in {"guest", "remixer"} for _name, role in credits):
        return False
    if _CREDIT_IN_TITLE.search(title or "") or _extract_remixers_from_title(title or ""):
        return False
    return _split_names(name for name, role in credits if role in {"main", "conductor"}) == album


def _split_names(names) -> frozenset[str]:
    return frozenset(strip_numbered_suffixes(part) for name in names for part in re_split(name))


def _tag_credits(tags: Any) -> list[tuple[str, str]]:
    """A file's credits as (name, role), read from its tags as salmon reads them: ARTIST (with a feat. or remix
    credit in it made a guest or remixer), every name in ARTISTS (the main artists, then the guests, as salmon writes
    it), and REMIXER. No credits at all when the file has no ARTIST."""
    if not tags.artist:
        return []
    credits = parse_artists(tags.artist)
    if artists := getattr(tags, "artists", None):
        credits += parse_artists(artists)
    credits += [(name, "remixer") for name in getattr(tags, "remixer", None) or []]
    return credits


def _names_several_discs(tags: Any) -> bool:
    """Whether a track's disc tags name more than one disc: a disc number over 1, or a total over 1, in DISCTOTAL
    or written after the disc number ("1/2", as a FLAC can hold it)."""
    number, _, total = str(tags.discnumber or "").partition("/")
    return (tag_number(tags, "disctotal") or 0) > 1 or any(
        part.strip().isdecimal() and int(part) > 1 for part in (number, total)
    )


def _listed_number(tags: Any, field: str) -> str:
    """A track or disc number as the tracklist gives it: "3/10" as "03"; one that is no number ("A1"), as written."""
    number = tag_number(tags, field)
    return str(getattr(tags, field) or "") if number is None else f"{number:02d}"


def generate_description(track_data: dict[str, Any], metadata: dict[str, Any]) -> str:
    """Generate group description with tracklist.

    A track whose tags credit only the album's artists is listed without them (#895): on a one-artist album, every
    track. A track that credits anyone else (another main artist, a guest or remixer, also in its title) keeps its
    artist, as do all tracks of a compilation, anthology, DJ mix or various-artists release, or when metadata gives
    no artists.

    Args:
        track_data: Track information.
        metadata: Release metadata.

    Returns:
        BBCode description string.
    """
    album = album_credit_names(metadata)
    description = "[b][size=4]Tracklist[/size][/b]\n"
    # Discs told apart by tags that aren't numbers ("CD1" and "CD2", or "1" and "CD2") are several discs too; those
    # are listed as written.
    unreadable_discs = {unreadable_number(t["t"], "discnumber") for t in track_data.values()} - {None}
    numbered_discs = {tag_number(t["t"], "discnumber") for t in track_data.values()} - {None}
    multi_disc = len(unreadable_discs | (numbered_discs if unreadable_discs else set())) > 1 or any(
        _names_several_discs(t["t"]) for t in track_data.values()
    )
    total_duration = 0
    for track in track_data.values():
        length = "{}:{:02d}".format(track["duration"] // 60, track["duration"] % 60)
        total_duration += track["duration"]
        if multi_disc:
            # A disc with no number is listed as the first.
            disc = _listed_number(track["t"], "discnumber") or "01"
            description += f"[b]{disc}-{_listed_number(track['t'], 'tracknumber')}.[/b] "
        else:
            description += f"[b]{_listed_number(track['t'], 'tracknumber')}.[/b] "

        if album is not None and credits_only_the_album(_tag_credits(track["t"]), track["t"].title, album):
            description += f"{track['t'].title} [i]({length})[/i]\n"
        else:
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
            bits = f"{track['precision']} bit / " if track["precision"] is not None else ""
            hybrid_info = f" [{bits}{track['sample rate'] / 1000} kHz]" if hybrid else ""
            tracklist += f"{os.path.splitext(filename)[0]} [i]({mins}:{secs:02d})[/i]{bitrate}{hybrid_info}\n"
        tracklist += "\n"

    lossy_notes = (
        f"[u]Lossy Notes:[/u]\n{lossy_comment}\n\n"
        if lossy_comment and cfg.upload.description.lma_comment_in_t_desc
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

    conversion = conversion_note or ""
    return (
        f"{spectrals}{encode_specifics}{release_date}{tracklist}{lossy_notes}{source}{more_info}{conversion}"
        f"{upload_footer()}"
    )


def upload_footer() -> str:
    """The line every torrent description salmon writes ends with."""
    return (
        f"[hr]Uploaded with [url=https://github.com/smokin-salmon/smoked-salmon]"
        f"[b]smoked-salmon[/b] v{get_version()}[/url]"
    )


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
