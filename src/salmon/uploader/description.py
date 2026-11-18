"""The group and torrent descriptions: the tracklist, its credit rules, and the source links and footer."""

import os
import re
from collections.abc import Sequence
from typing import Any, NamedTuple

from salmon import cfg
from salmon.common import re_split
from salmon.common.dsd import track_dsd_encoding
from salmon.common.numbers import tag_number, unreadable_number
from salmon.common.strings import (
    comparable,
    credit_in_title,
    join_artists,
    names_no_artist,
    reaches_various_threshold,
    strip_numbered_suffixes,
)
from salmon.release_notification import get_version
from salmon.sources import SOURCE_ICONS
from salmon.tagger.combine import _extract_remixers_from_title
from salmon.tagger.pre_data import parse_artists
from salmon.tagger.sources import METASOURCES, without_tracking
from salmon.uploader.spectrals import make_spectral_bbcode


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


# Release types whose lines all name their artist or all leave it out (#971): on a compilation, anthology or DJ mix,
# the artist is the point of each line, until all the lines would say the same.
_ALL_LINES_OR_NONE = frozenset({"Anthology", "Compilation", "DJ Mix"})


def album_credit_names(metadata: dict[str, Any]) -> frozenset[str] | None:
    """The names a track must credit, and no others, for the tracklist to leave its artist out (#895).

    They are the album's main artists and conductors, as ARTIST leads with them, each split as salmon splits the
    artists it reads from tags ("Simon & Garfunkel" is Simon and Garfunkel), so a credit read from the files and
    one from a store compare alike.

    Returns:
        The names, or None when every line keeps its artist: no album artists are given, or the album's main artists
        are various (as ALBUMARTIST would name them).
    """
    artists = metadata.get("artists") or []
    mains = [name for name, role in artists if role == "main"]
    if not mains:
        return None
    if reaches_various_threshold(mains) or any(names_no_artist(name) for name in mains):
        return None
    return _split_names([*mains, *(name for name, role in artists if role == "conductor")])


# A remix credit in a title, in any case: "Remix", "remixed" or "Remixes" as a whole word.
_REMIX_IN_TITLE = re.compile(r"\bremix(?:e[ds])?\b", flags=re.IGNORECASE)


def bare_title(
    credits: list[tuple[str, str]], title: str | None, album: frozenset[str], remix_said_once: bool = False
) -> str | None:
    """The title a track's tracklist line shows when it leaves the artist out, or None when the line keeps it.

    The line leaves it out when the track's main artists and conductors, as (name, role), are exactly the album's
    (see album_credit_names), and anyone else it credits is a guest (#971 item 6): the title then names them,
    "Song (feat. B)". A remix keeps the artist, read from REMIXER or the title (where salmon puts a remixer, and an
    AAC file has no REMIXER), unless the album's one remixer is said once instead (`remix_said_once`, #971 item 5);
    so does a title that may credit someone the tags don't ("Song (with B)").

    Its other roles (composer, producer, arranger) never show in the tracklist, so they don't count.
    """
    title = title or ""
    if not remix_said_once and (_remixers(credits, title) or _REMIX_IN_TITLE.search(title)):
        return None
    if _lead_names(credits) != album:
        return None
    guests = [name for name, role in credits if role == "guest"]
    if not guests:
        return None if credit_in_title(title) else title
    return _with_guests(title, guests) if title else None


# A remix in a title, which a guest goes before: "Song (feat. B) (X Remix)".
_REMIX_BRACKET = re.compile(r" \([^)]+Remix(?:er)?\)", flags=re.IGNORECASE)


def _with_guests(title: str, guests: list[str]) -> str | None:
    """The title naming its guests, as guests_in_track_title puts them but with every name (the line is where they
    show): unchanged when it already names them, and None, the line keeping its artist, when it credits someone but
    not every guest ("Song (feat. B)" for guests B and C)."""
    if credit_in_title(title, guests):
        return title if _names_each(title, guests) else None
    remix = _REMIX_BRACKET.search(title)
    head, tail = (title[: remix.start()] + title[remix.end() :], remix[0]) if remix else (title, "")
    return f"{head} (feat. {join_artists(guests)}){tail}"


def _names_each(title: str, names: list[str]) -> bool:
    """Whether a title names each of these, loosely: "Song (feat. B & C)" names B and C."""
    plain = comparable(title)
    return all(comparable(part) in plain for name in names for part in re_split(name))


def _remixers(credits: list[tuple[str, str]], title: str | None) -> list[str]:
    """The remixers a track credits, in REMIXER or as salmon reads them from its title ("Song (X Remix)")."""
    pairs = [*credits, *_extract_remixers_from_title(title or "")]
    return list(dict.fromkeys(name for name, role in pairs if role == "remixer"))


def album_remixers(tracks: Sequence[tuple[list[tuple[str, str]], str]]) -> list[str] | None:
    """The remixers every track credits (#971 item 5), as the first names them: None unless there are several tracks
    and each credits the same ones."""
    first = _remixers(*tracks[0]) if len(tracks) > 1 else []
    if first and all(_split_names(_remixers(*track)) == _split_names(first) for track in tracks):
        return first
    return None


def listed_titles(
    tracks: Sequence[tuple[list[tuple[str, str]], str]], album: frozenset[str] | None, rls_type: str | None
) -> tuple[list[str | None], list[str] | None]:
    """For each track, as (its credits, its title), the title its tracklist line shows without the artist, or None
    when the line keeps it (#895, see bare_title); and the album's one remixer, said once when every line leaves the
    artist out (#971 item 5). On a compilation, anthology or DJ mix, either every line leaves it out or none does."""
    if album is None:
        return [None] * len(tracks), None
    if remixers := album_remixers(tracks):
        titles = [bare_title(credits, title, album, remix_said_once=True) for credits, title in tracks]
        if None not in titles:
            return titles, remixers
    titles = [bare_title(credits, title, album) for credits, title in tracks]
    if rls_type in _ALL_LINES_OR_NONE and None in titles:
        return [None] * len(tracks), None
    return titles, None


def _lead_names(credits: list[tuple[str, str]]) -> frozenset[str]:
    return _split_names(name for name, role in credits if role in {"main", "conductor"})


def _split_names(names) -> frozenset[str]:
    return frozenset(strip_numbered_suffixes(part) for name in names for part in re_split(name))


def _tag_credits(tags: Any, album: frozenset[str] | None) -> list[tuple[str, str]]:
    """A file's credits as (name, role), read from its tags as salmon reads them: ARTIST (with a feat. or remix
    credit in it made a guest or remixer), the names in ARTISTS (the main artists, then the guests, as salmon writes
    it), and REMIXER. When ARTIST alone is the album's credit, a plain name in ARTISTS credits no one more: it may be
    a band's member (#971), unless the title credits them, as guests_in_track_title writes a guest. No credits at all
    when the file has no ARTIST."""
    if not tags.artist:
        return []
    credits = parse_artists(tags.artist)
    if artists := getattr(tags, "artists", None):
        artist_is_the_album = _lead_names(credits) == album
        for name, role in parse_artists(artists):
            if not (artist_is_the_album and role == "main"):
                credits.append((name, role))
            elif credit_in_title(tags.title or "", [name]) and _names_each(tags.title or "", [name]):
                credits.append((name, "guest"))
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


class TracklistLine(NamedTuple):
    """One line of a group description's tracklist, as ``tracklist_description`` writes it."""

    number: str  # "01", or "01-03" when the release has several discs
    title: str
    artist: str | None  # None: the line leaves the artist out (#895)
    seconds: int | None  # None: the source gave no length


def _clock(seconds: int) -> str:
    """A length as a tracklist gives it, in minutes and seconds: 3723 is "62:03"."""
    return f"{seconds // 60}:{seconds % 60:02d}"


def tracklist_description(
    lines: Sequence[TracklistLine], comment: str | None, urls: list[str] | None, remixers: list[str] | None = None
) -> str:
    """A group description: the tracklist, who remixed every track, its total length, the comment and the links to
    the release's sources.

    It knows nothing of tags or stores: each caller reads its own numbers, artists and lengths into the lines.

    Args:
        lines: The tracklist, in order.
        comment: A note to add after the tracklist, if any.
        urls: The release's source links, if any.
        remixers: The remixers of every track, said once after the tracklist (#971), if any.

    Returns:
        BBCode description string. The total is shown for more than one line, and only when every line has a
        length, as a total of some of them would be wrong.
    """
    description = "[b][size=4]Tracklist[/size][/b]\n"
    for line in lines:
        credit = "" if line.artist is None else f"{line.artist} - "
        length = "" if line.seconds is None else f" [i]({_clock(line.seconds)})[/i]"
        description += f"[b]{line.number}.[/b] {credit}{line.title}{length}\n"
    if remixers:
        description += f"\nAll tracks remixed by {format_tracklist_artists(remixers)}.\n"

    known = [line.seconds for line in lines if line.seconds is not None]
    if len(lines) > 1 and len(known) == len(lines):
        description += f"\n[b]Total length: [/b]{_clock(sum(known))}\n"

    if comment:
        description += f"\n{comment}\n"

    if urls:
        description += "\n[b]More info:[/b] " + generate_source_links(urls)

    return description


def generate_description(track_data: dict[str, Any], metadata: dict[str, Any]) -> str:
    """Generate group description with tracklist.

    A track whose tags credit only the album's artists is listed without them (#895): on a one-artist album, every
    track. A guest of theirs goes after the title, "Song (feat. B)", and one remixer of every track is said once
    (#971, see listed_titles). A track that credits anyone else (another main artist, or a remixer, also in its title)
    keeps its artist, as do all tracks of a various-artists release, or when metadata gives no artists, and all tracks
    of a compilation, anthology or DJ mix unless every one is listed without it (#971).

    Args:
        track_data: Track information.
        metadata: Release metadata.

    Returns:
        BBCode description string.
    """
    album = album_credit_names(metadata)
    # Discs told apart by tags that aren't numbers ("CD1" and "CD2", or "1" and "CD2") are several discs too; those
    # are listed as written.
    unreadable_discs = {unreadable_number(t["t"], "discnumber") for t in track_data.values()} - {None}
    numbered_discs = {tag_number(t["t"], "discnumber") for t in track_data.values()} - {None}
    multi_disc = len(unreadable_discs | (numbered_discs if unreadable_discs else set())) > 1 or any(
        _names_several_discs(t["t"]) for t in track_data.values()
    )
    titles, remixers = listed_titles(
        [(_tag_credits(track["t"], album), track["t"].title) for track in track_data.values()],
        album,
        metadata.get("rls_type"),
    )
    lines: list[TracklistLine] = []
    for track, bare in zip(track_data.values(), titles, strict=True):
        number = _listed_number(track["t"], "tracknumber")
        if multi_disc:
            # A disc with no number is listed as the first.
            number = f"{_listed_number(track['t'], 'discnumber') or '01'}-{number}"

        if bare is None:
            title, artist = track["t"].title, format_tracklist_artists(track["t"].artist)
        else:
            title, artist = bare, None
        lines.append(TracklistLine(number, title, artist, track["duration"]))

    return tracklist_description(lines, metadata["comment"], metadata["urls"], remixers)


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
            if dsd := track_dsd_encoding(track):
                # "DSD64 2.8224 MHz", not "1 bit 2822.4 kHz".
                mhz = f"{track['sample rate'] / 1_000_000:g}"
                encode_specifics = f"{prefix} [b]{dsd} [color=#2E86C1]{mhz}[/color] MHz[/b]\n"
            else:
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
            bitrate = f" [{track['bit rate'] / 1000:.01f}kbps]" if cfg.upload.description.bitrates_in_t_desc else ""
            bits = f"{track['precision']} bit / " if track["precision"] is not None else ""
            hybrid_info = f" [{bits}{track['sample rate'] / 1000} kHz]" if hybrid else ""
            tracklist += f"{os.path.splitext(filename)[0]} [i]({_clock(track['duration'])})[/i]{bitrate}{hybrid_info}\n"
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
    # The source link's, then each one listed: two forms of one store link list it once.
    shown = {without_tracking(source_url.strip())} if source_url else set()

    for url in metadata_urls:
        normalized_url = without_tracking(url.strip())
        if normalized_url in shown:
            continue
        shown.add(normalized_url)

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
