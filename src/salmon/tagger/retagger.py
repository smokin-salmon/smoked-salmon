import os
import re
import shutil
from collections import Counter
from itertools import chain
from string import Formatter
from typing import Any

import asyncclick as click
import msgspec
from mutagen import mp4

from salmon import cfg, interaction
from salmon.common import clean_name, has_visible_chars
from salmon.common.strings import artists_or_various, join_artists, reaches_various_threshold
from salmon.constants import (
    ARROWS,
    BLACKLISTED_CHARS,
    BLACKLISTED_FULLWIDTH_REPLACEMENTS,
)
from salmon.errors import AmbiguousTrackOrderError, NumberedTrackMoveError, UploadError
from salmon.tagger.pairing import BY_ISRC_OR_TITLE, pair_by_content
from salmon.tagger.renames import RenameStopped, plan_renames, rename_all
from salmon.tagger.tagfile import TagFile


class Change(msgspec.Struct, frozen=True):
    """Data structure for tag changes."""

    tag: str
    old: Any
    new: Any


async def tag_files(path, tags, metadata, auto_rename, *, clear=None):
    """
    Wrapper function that calls the functions that create and print the
    proposed changes, and then prompts for confirmation to retag the file.

    ``clear`` maps the album fields an earlier pass wrote and this metadata no longer has to the value it wrote:
    they are removed.

    Returns each retagged file's track, by filename, or None when the files were not retagged.
    """
    clear = clear or {}
    click.secho("\nRetagging files...", fg="cyan", bold=True)
    if not check_whether_to_tag(tags, metadata):
        return None
    album_changes = collect_album_data(metadata)
    try:
        pairs = pair_files_with_tracks(tags, metadata)
    except AmbiguousTrackOrderError as e:
        click.secho(str(e), fg="red")
        click.secho("Skipping retagging procedure...", fg="red")
        return None
    except NumberedTrackMoveError as e:
        if not await _confirm_moves(e.moves):
            click.secho("Skipping retagging procedure...", fg="red")
            return None
        pairs = pair_files_with_tracks(tags, metadata, move_numbered=True)
    track_changes = _track_changes(pairs)
    print_changes(album_changes, track_changes, next(iter(tags.values())), clear=clear)
    if (
        auto_rename
        or await interaction.assume_defaults()
        or await interaction.confirm(
            click.style("\nWould you like to auto-tag the files with the updated metadata?", fg="magenta"),
            default=True,
        )
    ):
        retag_files(path, album_changes, track_changes, clear=clear)
        return {filename: trackmeta for filename, _tagset, trackmeta in pairs}
    return None


async def _confirm_moves(moves):
    """Ask before giving numbered files other tracks' tags. A run that answers with defaults (-yyy) doesn't."""
    click.secho("\nThese files carry their own track numbers, but the metadata pairs them with other tracks:", fg="red")
    for filename, numbered_as, pairs_with, reason in moves:
        click.secho(
            f"  {filename}: numbered {_track_label(numbered_as)}, "
            f"would be {_track_label(pairs_with)}. {pairs_with['title']} (matched on {reason})",
            fg="red",
        )
    if await interaction.assume_defaults():
        click.secho("Not retagging numbered files as other tracks without asking.", fg="red")
        return False
    return await interaction.confirm(click.style("Retag them as those tracks?", fg="magenta"), default=False)


def _own_number(tagset):
    """The file's own DISCNUMBER and TRACKNUMBER, shaped like a track for ``_track_label``."""
    disc = _parse_tag_number(tagset, "discnumber")
    return {"disc#": None if disc is None else str(disc), "track#": str(_parse_tag_number(tagset, "tracknumber"))}


def _track_label(track):
    disc = track.get("disc#")
    return f"{disc}-{track['track#']}" if disc and str(disc) != "1" else str(track["track#"])


def check_whether_to_tag(tags, metadata):
    """
    Make sure the number of tracks in the metadata equals the number of tracks
    in the folder.
    """
    if len(tags) != sum([len(disc) for disc in metadata["tracks"].values()]):
        click.secho(
            "Number of tracks differed from number of tracks in metadata, skipping retagging procedure...",
            fg="red",
        )
        return False
    return True


def collect_album_data(metadata):
    """Create a dictionary of the proposed album tags (consistent across every track)."""
    if cfg.upload.formatting.add_edition_title_to_album_tag and metadata["edition_title"]:
        title = f"{metadata['title']} ({metadata['edition_title']})"
    else:
        title = metadata["title"]
    return {
        k: v
        for k, v in {
            "album": title,
            "genre": sorted(metadata["genres"]),
            "date": metadata["group_year"],
            "label": metadata["label"],
            "catno": metadata["catno"],
            "albumartist": _generate_album_artist(metadata["artists"]),
            "upc": metadata["upc"],
            "comment": metadata["comment"] if cfg.upload.formatting.review_as_comment_tag else None,
        }.items()
        if v
    }


def _generate_album_artist(artists):
    return artists_or_various(a for a, i in artists if i == "main")


def create_track_changes(tags, metadata, *, move_numbered=False):
    """
    Compare the track data in the metadata to the track data in the tags
    and record all differences. Raises as ``pair_files_with_tracks`` does.
    """
    return _track_changes(pair_files_with_tracks(tags, metadata, move_numbered=move_numbered))


def pair_files_with_tracks(tags, metadata, *, move_numbered=False):
    """Each file as (filename, tagset, track): the metadata track whose tags it gets.

    Raises AmbiguousTrackOrderError when the files' order can't be trusted and the content can't place them, and
    NumberedTrackMoveError when files that carry their own track numbers would get other tracks' tags (their ISRC
    or title says so), unless `move_numbered`.
    """
    tracks = metadata_to_track_list(metadata["tracks"])
    ambiguous = None

    def disc_track_key(tagset):
        return (_get_tag_number(tagset, "discnumber"), _get_tag_number(tagset, "tracknumber"))

    # An unparseable tag reads as 1 via _get_tag_number, so it can't vouch for a file's place: only
    # a disc/track pair built from tags we actually trust identifies a file, and TRACKNUMBER must
    # be present and parseable everywhere either way.
    tracknumber_readable = all(_parse_tag_number(tagset, "tracknumber") is not None for tagset in tags.values())
    disc_track_keys = [disc_track_key(tagset) for tagset in tags.values()]
    keys_unique = len(set(disc_track_keys)) == len(disc_track_keys)
    discnumber_tags = [_has_tag(tagset, "discnumber") for tagset in tags.values()]

    # DISCNUMBER on no file keys every file (1, track), which proves the track order: all the zip below needs.
    # On every file, it must parse; on only some, a file without it would sort as disc 1 among real disc 1s.
    disc_tags_usable = not any(discnumber_tags) or (
        all(discnumber_tags) and all(_parse_tag_number(tagset, "discnumber") is not None for tagset in tags.values())
    )
    if tracknumber_readable and keys_unique and disc_tags_usable and _zips_onto(disc_track_keys, metadata["tracks"]):
        ordered_tags = sorted(tags.items(), key=lambda item: disc_track_key(item[1]))
    else:
        try:
            ordered_tags = _order_by_disc_folders(tags, metadata["tracks"])
        except AmbiguousTrackOrderError as e:
            ordered_tags, ambiguous = list(tags.items()), e

    # Pair each file with its own track by ISRC and title rather than by position, so unnumbered
    # promo folders tag correctly. The order above is the fallback for files the content can't place.
    # Files that all carry their own numbers keep them unless an ISRC or exact title says otherwise,
    # and then only when asked. With no trustworthy order, every file must be placed by ISRC or title,
    # and a numbered file is asked about when it would get a disc and track other than its own tags'.
    numbered = _numbered_by_tags(tags)
    by_order = tracks
    tracks, file_reasons = order_tracks_for_files(
        ordered_tags, tracks, numbered=numbered and ambiguous is None, show=not numbered
    )
    if ambiguous is not None and not all(reason in BY_ISRC_OR_TITLE for reason in file_reasons):
        raise ambiguous
    moves = [
        (filename, _own_number(tagset), pairs_with, reason)
        for (filename, tagset), in_order, pairs_with, reason in zip(
            ordered_tags, by_order, tracks, file_reasons, strict=False
        )
        if (pairs_with is not in_order if ambiguous is None else disc_track_key(tagset) != _track_key(pairs_with))
    ]
    if numbered and moves and not move_numbered:
        raise NumberedTrackMoveError(moves)
    return [(filename, tagset, trackmeta) for (filename, tagset), trackmeta in zip(ordered_tags, tracks, strict=False)]


def _track_changes(pairs):
    """The changes that give each file of ``pair_files_with_tracks`` its track's tags."""
    changes = {}
    for filename, tagset, trackmeta in pairs:
        changes[filename] = []

        try:
            old_artist_str = ", ".join(tagset.artist)
        except TypeError:
            old_artist_str = "None"

        new_artist_str = create_artist_str(trackmeta["artists"])
        if old_artist_str != new_artist_str:
            changes[filename].append(Change("artist", old_artist_str, new_artist_str))

        if not _is_aac(tagset):
            old_artists = list(getattr(tagset, "artists", None) or [])
            new_artists = create_artists_list(trackmeta["artists"])
            if new_artists and old_artists != new_artists:
                changes[filename].append(Change("artists", old_artists or None, new_artists))

        old_composer = getattr(tagset, "composer", None) or "None"
        new_composer = create_composer_str(trackmeta["artists"])
        if new_composer and old_composer != new_composer:
            changes[filename].append(Change("composer", old_composer, new_composer))

        old_conductor = getattr(tagset, "conductor", None) or "None"
        new_conductor = create_conductor_str(trackmeta["artists"])
        if new_conductor and old_conductor != new_conductor:
            changes[filename].append(Change("conductor", old_conductor, new_conductor))

        if cfg.upload.formatting.guests_in_track_title:
            trackmeta["title"] = append_guests_to_track_titles(trackmeta)

        if cfg.upload.formatting.empty_track_comment_tag and getattr(tagset, "comment", False):
            changes[filename].append(Change("comment", tagset.comment, ""))

        for tagfield, metafield in [
            ("title", "title"),
            ("isrc", "isrc"),
            ("tracknumber", "track#"),
            ("discnumber", "disc#"),
            ("tracktotal", "tracktotal"),
            ("disctotal", "disctotal"),
        ]:
            change = _compare_tag(tagfield, metafield, tagset, trackmeta)
            if change:
                changes[filename].append(change)
    return changes


# A feature credit already in a title, as a whole word in any case: "feat", "Feat.", "featuring" or "ft."
_FEATURE_CREDIT = re.compile(r"\b(?:feat|featuring|ft)\b", flags=re.IGNORECASE)


def append_guests_to_track_titles(track):
    guest_artists = [a for a, i in track["artists"] if i == "guest"]
    # This looked for "feat" anywhere, so "Song (Feat. Alice)" gained a second credit and "Defeat" none
    if not _FEATURE_CREDIT.search(track["title"]) and guest_artists:
        # If we find a remix parenthetical, remove it and re-add it after the guest artists.
        remix = re.search(r"( \([^\)]+Remix(?:er)?\))", track["title"], flags=re.IGNORECASE)
        if remix:
            track["title"] = track["title"].replace(remix[1], "")
        # More than various_artist_threshold guests used to be left out here, and so from ARTIST, which leaves guests
        # to the title
        track["title"] += f" (feat. {artists_or_various(guest_artists)})"
        if remix:
            track["title"] += remix[1]
    return track["title"]


def metadata_to_track_list(metadata):
    """Turn the double nested dictionary of tracks into a flat list of tracks, discs in natural order."""
    return list(chain.from_iterable(metadata[disc].values() for disc in sorted(metadata, key=_disc_track_sort_key)))


def _track_key(track):
    """A track's (disc, track) pair, read the way a file's own tags are (no disc is disc 1)."""
    return (_to_number(track.get("disc#") or 1), _to_number(track["track#"]))


def _to_number(value):
    """A digit string read as an int, so it compares equal to the number a tag parses to; anything else as is.

    ``str.isdecimal()``, not ``str.isdigit()``: ``isdigit()`` accepts some Unicode digits (superscript "2")
    that ``int()`` then rejects, while ``isdecimal()`` is true for exactly what ``int()`` accepts.
    """
    s = str(value)
    return int(s) if s.isdecimal() else s


def _zips_onto(keys, discs) -> bool:
    """Whether files sorted by their (disc, track) keys line up with the metadata's discs in order.

    They do when either side has one disc (a flat tracklist, or one tagged disc numbered straight through),
    or when each tagged disc holds as many files as the metadata's disc in the same place has tracks.
    """
    file_discs = Counter(disc for disc, _track in keys)
    if len(file_discs) == 1 or len(discs) == 1:
        return True
    return [file_discs[disc] for disc in sorted(file_discs)] == [
        len(discs[disc]) for disc in sorted(discs, key=_disc_track_sort_key)
    ]


def _order_by_disc_folders(tags, discs):
    """Pair files whose disc/track tags collide one folder per disc, or raise when that can't identify each file.

    Files are grouped by the folder they are in, folders are taken as discs in natural order (CD2 before
    CD10), and each folder is ordered against the corresponding disc by its files' track tags. If the folder
    layout does not resolve to one folder per disc with the right number of tracks, we refuse rather than
    guess: silently mispairing files with the wrong disc's titles is worse than stopping the retag.
    """
    by_path = sorted(tags.items(), key=lambda item: _natural_key(item[0]))
    if len(by_path) != sum(len(tracks) for tracks in discs.values()):
        return by_path  # the caller reports the track count mismatch
    folders: dict[str, list] = {}
    for item in by_path:
        folders.setdefault(os.path.dirname(item[0]), []).append(item)
    groups = [folders[folder] for folder in sorted(folders, key=_natural_key)]
    disc_sizes = [len(discs[disc]) for disc in sorted(discs, key=_disc_track_sort_key)]
    if not _names_distinct(folders) or [len(group) for group in groups] != disc_sizes:
        raise _ambiguous_tracks()
    return [item for group in groups for item in _order_within_disc(group)]


def _order_within_disc(group):
    """Order one disc folder's files: by track tag when every file has its own distinct one, else by file name.

    This is also the path a single-disc, single-folder release takes, so a folder full of duplicate or
    missing TRACKNUMBER tags (exactly why someone would retag) still resolves by file name instead of being
    refused. Names are an order only when every one starts with a number, or when unique track tags pin
    all files but one and the name order keeps each pinned file at its tag's place: "Apple" before "Zebra"
    says nothing about which is track 1. Raise otherwise.
    """
    numbers = [_parse_tag_number(tagset, "tracknumber") for _, tagset in group]
    if None not in numbers and len(set(numbers)) == len(numbers):
        return sorted(group, key=lambda item: _get_tag_number(item[1], "tracknumber"))
    names = [filename for filename, _ in group]
    if not _names_distinct(names):
        raise _ambiguous_tracks()
    by_name = sorted(group, key=lambda item: _natural_key(item[0]))
    if all(re.match(r"\d", os.path.basename(name)) for name in names):
        return by_name
    pinned = sum(1 for n in numbers if n is not None and numbers.count(n) == 1)
    if pinned >= len(group) - 1 and not _names_move_a_pinned_file(by_name):
        return by_name
    raise _ambiguous_tracks()


def _names_move_a_pinned_file(by_name) -> bool:
    """Whether name order puts a file away from the place its track tag gives, when no other file has that tag."""
    numbers = [_parse_tag_number(tagset, "tracknumber") for _, tagset in by_name]
    return any(n is not None and numbers.count(n) == 1 and n != place for place, n in enumerate(numbers, 1))


def _names_distinct(names) -> bool:
    """Whether natural order tells these names apart (``01.flac`` and ``1.flac`` tie, as do CD01 and CD1)."""
    keys = [tuple(_natural_key(name)) for name in names]
    return len(set(keys)) == len(keys)


def _ambiguous_tracks() -> AmbiguousTrackOrderError:
    """The error for a retag whose files the tags and folders can't pair with tracks."""
    return AmbiguousTrackOrderError(
        "Can't tell which file is which track: some files share a disc and track number, or lack one, and "
        "neither the tags nor a folder per disc sort them out. Fix their DISCNUMBER and TRACKNUMBER tags, or "
        "put each disc in its own folder, before retagging."
    )


def _natural_key(path: str) -> list[int | str]:
    """Sort key that compares the digit runs in a path as numbers, so CD2 sorts before CD10.

    Uses ``str.isdecimal()``, not ``str.isdigit()``: ``\\d+`` in the regex only matches ASCII-like decimal
    digits, but a lone non-decimal digit character next to them (superscript "2" in "01²2.flac") still ends
    up as its own split segment, where ``isdigit()`` would accept it and ``int()`` would then reject it.
    """
    return [int(part) if part.isdecimal() else part.lower() for part in re.split(r"(\d+)", path)]


def _disc_track_sort_key(value):
    """Sort key that treats a numeric string as a number, so disc "10" sorts after "2"."""
    s = str(value)
    return (0, int(s)) if s.isdecimal() else (1, s.lower())


def _tagset_identity(tagset):
    """The signals used to recognise which scraped track a file is."""

    def tag(field):
        value = tagset.get(field) if isinstance(tagset, dict) else getattr(tagset, field, None)
        return value[0] if isinstance(value, list) and value else value or None

    mut = getattr(tagset, "mut", None)
    info = getattr(mut, "info", None)
    return {
        "title": tag("title"),
        "isrc": tag("isrc"),
        "duration": int(info.length) if info and getattr(info, "length", None) else None,
    }


def order_tracks_for_files(items, tracks, *, numbered=False, show=True):
    """Reorder ``tracks`` so that entry N belongs to the Nth file in ``items``.

    ``items`` is the ``(filename, tagset)`` pairs in the order they would otherwise be
    zipped with ``tracks``. ``combine_tracks`` already paired the tracks by content, but it
    works on track dicts that carry no filename, so the association is lost by the time tags
    are written. Redo the match here against the files themselves: a track that
    ``combine_tracks`` paired by ISRC or title now carries that ISRC or the scraped title, and
    the duration of the local file it was built from (``create_track_list``), which breaks
    ties between titles that fit two tracks the same way the first match did.

    With ``numbered``, the files are in the order of their own track numbers, and only an ISRC
    or an exact title moves one (see ``pair_by_content``). With ``show``, the matches are printed
    when content reordered anything; the caller shows, and asks about, a numbered file's move instead.

    Returns the reordered tracks and how each file was paired. On a count mismatch or an
    incomplete pairing ``tracks`` comes back as is, every file paired by position.
    """
    by_position = (tracks, ["position"] * len(items))
    if len(items) != len(tracks):
        return by_position  # Counts disagree: keep the order as it is, and the caller reports the mismatch.
    locals_ = [_tagset_identity(tagset) for _, tagset in items]
    pairing, reasons = pair_by_content(locals_, tracks, numbered=numbered)

    slots: list[dict | None] = [None] * len(items)
    for mi, li in enumerate(pairing):
        if li is not None:
            slots[li] = tracks[mi]
    ordered = [t for t in slots if t is not None]
    if len(ordered) != len(slots):
        return by_position  # Incomplete pairing; don't risk a partial reorder.

    file_reasons = _reasons_by_file(pairing, reasons)
    if ordered != tracks and show:
        click.secho("\nMatched files to the metadata tracklist by content:", fg="yellow", bold=True)
        for (filename, _), track, reason in zip(items, ordered, file_reasons, strict=True):
            click.secho(
                f"  {filename}\n      -> {track['track#']}. {track['title']} (matched on {reason})", fg="yellow"
            )
    return ordered, file_reasons


def _numbered_by_tags(tags):
    """Whether every file carries its own track number: one above zero, which no other file of its folder and
    disc shares (a promo folder whose files are all TRACKNUMBER 1 is not numbered)."""
    numbers = {}
    for filename, tagset in tags.items():
        number = _parse_tag_number(tagset, "tracknumber")
        if number is None or number < 1:
            return False
        numbers.setdefault((os.path.dirname(filename), _parse_tag_number(tagset, "discnumber")), []).append(number)
    return all(len(set(group)) == len(group) for group in numbers.values())


def _reasons_by_file(pairing, reasons):
    """Re-key the per-scraped-track reasons onto the local file order."""
    by_file = [None] * len(pairing)
    for mi, li in enumerate(pairing):
        if li is not None:
            by_file[li] = reasons[mi]
    return by_file


def _compare_tag(tagfield, metafield, tagset, trackmeta):
    """
    Compare a tag to the equivalent metadata field. If the metadata field
    does not equal the existing tag, return a ``Change``.
    """
    if trackmeta[metafield]:
        if not getattr(tagset, tagfield, False):
            return Change(tagfield, None, trackmeta[metafield])
        if str(getattr(tagset, tagfield, "")) != str(trackmeta[metafield]):
            return Change(tagfield, getattr(tagset, tagfield, "None"), trackmeta[metafield])
    return None


def create_artist_str(artists):
    """Create the artist string from the metadata.

    For classical-friendly tagging, conductor roles are included in the ARTIST
    tag after the main performer list, while composer roles are excluded and
    written to their own COMPOSER tag.
    """
    main_artists = _ordered_unique(a for a, i in artists if i == "main")
    conductors = _ordered_unique(a for a, i in artists if i == "conductor")
    lead_artists = _ordered_unique([*main_artists, *conductors])

    # ALBUMARTIST's separator rule. This used " & " whenever a name contained "&", so "Simon & Garfunkel" and
    # "Paul Desmond" became "Simon & Garfunkel & Paul Desmond"
    artist_str = ", ".join(lead_artists) if conductors else join_artists(lead_artists)

    if not cfg.upload.formatting.guests_in_track_title:
        guest_artists = [a for a, i in artists if i == "guest"]
        if guest_artists:
            artist_str += f" (feat. {artists_or_various(guest_artists)})"

    return artist_str


def create_artists_list(artists):
    """The track's artists by name, one per value of the ARTISTS tag: its main artists, then its guests.

    Unlike ``create_artist_str`` this never swaps guests for the various artists word or leaves them to the
    title, and has no conductors, composers or remixers: those are not the track's credited artists.
    """
    return _ordered_unique([*(a for a, i in artists if i == "main"), *(a for a, i in artists if i == "guest")])


def _is_aac(tagset) -> bool:
    """Whether the tags are an M4A's, which has no ARTISTS tag and takes a genre as one string."""
    return isinstance(getattr(tagset, "mut", None), mp4.MP4)


def create_composer_str(artists):
    """Create the composer string from the metadata."""
    composers = _ordered_unique(a for a, i in artists if i == "composer")
    return ", ".join(composers)


def create_conductor_str(artists):
    """Create the conductor string from the metadata."""
    conductors = _ordered_unique(a for a, i in artists if i == "conductor")
    return ", ".join(conductors)


def _ordered_unique(values):
    """Preserve the first-seen order while removing duplicates."""
    return list(dict.fromkeys(values))


def print_changes(album_changes, track_changes, a_track, clear=None):
    """Print all the proposed track changes, then all the album data."""
    if any(t for t in track_changes.values()):
        click.secho("\nProposed tag changes:", fg="yellow", bold=True)
    for filename, changes in track_changes.items():
        if changes:
            click.secho(f"> {filename}", fg="yellow")
            for change in changes:
                old, new = _shown(change.old, change.new)
                click.echo(f"  {change.tag.ljust(20)} ••• {old} {ARROWS} {new}")

    click.secho("\nAlbum tags (applied to all):", fg="yellow", bold=True)
    for field, value in album_changes.items():
        previous = getattr(a_track, field, None)
        if isinstance(value, list):
            # What the file would hold, so the values are compared as the file keeps them, not as text
            value = _written(a_track, value)
            previous = _values(previous) if previous is not None else None
            is_different = (previous or []) != _values(value)
        else:
            is_different = _joined(previous) != str(value)
        previous, value = _shown(previous, value)
        if not is_different:
            click.secho(f"> {field.ljust(13)} ••• {previous}")
        else:
            click.echo(
                f"> {click.style(str(field.ljust(13)), bold=True)} ••• {str(previous)} "
                f"{ARROWS} {click.style(str(value), bold=True)}"
            )
    for field, previous in sorted((clear or {}).items()):
        click.echo(
            f"> {click.style(str(field.ljust(13)), bold=True)} ••• {_joined(previous)} "
            f"{ARROWS} {click.style('(removed)', bold=True)}"
        )


def _values(value):
    """The values a tag holds, as a list of strings."""
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _shown(old, new):
    """The two sides of a change as text.

    The values of a list are joined with a semicolon. When two different lists would read the same that
    way (one value ``Hip Hop; Rap`` and the two values ``Hip Hop`` and ``Rap``), each value is quoted instead.
    """
    if isinstance(old, list) and isinstance(new, list) and old != new and _joined(old) == _joined(new):
        return _quoted(old), _quoted(new)
    return _joined(old), _joined(new)


def _joined(value):
    return "; ".join(map(str, value)) if isinstance(value, list) else str(value)


def _quoted(values):
    return ", ".join(f'"{v}"' for v in values)


def _written(mut, value):
    """What a file takes for a tag value: FLAC and MP3 hold one value per list item, M4A one string."""
    if isinstance(value, list):
        return "; ".join(map(str, value)) if _is_aac(mut) else [str(v) for v in value]
    return str(value)


def retag_files(path, album_changes, track_changes, clear=()):
    """Apply the proposed metadata changes to the files."""
    for filename, changes in track_changes.items():
        mut = TagFile(os.path.join(path, filename))
        for change in changes:
            if change.tag == "artists" and _is_aac(mut):
                continue
            setattr(mut, change.tag, _written(mut, change.new))
        for tag, value in album_changes.items():
            setattr(mut, tag, _written(mut, value))
        for tag in clear:
            mut.remove(tag)
        mut.save()
    click.secho("Retagged files.", fg="green")


async def rename_files(path, tags, metadata, auto_rename, spectral_ids, source=None, tracks_by_file=None):
    """
    Call functions that generate the proposed changes, then print and prompt
    for confirmation. Apply the changes if user agrees.

    ``tracks_by_file`` is what ``tag_files`` returns: with it, a file's {artist} is the various artists word
    when its track has that many main artists.
    """
    to_rename = []
    folders_to_create = set()
    multi_disc = len(metadata["tracks"]) > 1
    md_word = {"CD": "CD", "Vinyl": "LP"}.get(source or "", "Part")
    # "Part" is default if not CD or Vinyl
    # Keep a multi-disc release in the release folder, its tracks numbered <disc>.<track>
    single_folder = multi_disc and not cfg.upload.formatting.split_multi_disc_into_folders
    # Disc numbers of the tracks in each folder that is emptied into the release folder
    folder_discs = {}

    track_list = list(chain.from_iterable([d.values() for d in metadata["tracks"].values()]))
    multiple_artists = any(
        {a for a, i in t["artists"] if i == "main"} != {a for a, i in track_list[0]["artists"] if i == "main"}
        for t in track_list[1:]
    )

    # In one folder, every disc and track number is padded to the width of the largest, so the files sort by
    # disc, then track
    disc_digits = len(str(max((_get_tag_number(t, "discnumber") for t in tags.values()), default=1)))
    track_digits = max(2, len(str(max((_get_tag_number(t, "tracknumber") for t in tags.values()), default=1))))

    for filename, tracktags in tags.items():
        ext = os.path.splitext(filename)[1].lower()
        # Counted from the track's main artists, as the folder name is; a file not retagged keeps its ARTIST
        track = (tracks_by_file or {}).get(filename)
        various = track is not None and reaches_various_threshold(a for a, i in track["artists"] if i == "main")
        new_name = generate_file_name(tracktags, ext, multiple_artists, various=various)
        disc_number = 1  # Default value
        if single_folder:
            disc_number = _get_tag_number(tracktags, "discnumber")
            track_number = _get_tag_number(tracktags, "tracknumber")
            new_name = generate_file_name(
                tracktags,
                ext,
                multiple_artists,
                trackno_or=f"{disc_number:0{disc_digits}d}.{track_number:0{track_digits}d}",
                various=various,
            )
            folder = os.path.dirname(os.path.join(path, filename))
            if folder != path:
                folder_discs.setdefault(folder, set()).add(disc_number)
        elif multi_disc:
            if isinstance(tracktags, dict):
                disc_number = int(tracktags["discnumber"][0].split("/")[0]) if "discnumber" in tracktags else 1
            else:
                disc_number = int(tracktags.discnumber.split("/")[0]) or 1
            new_name = os.path.join(f"{md_word}{disc_number:02d}", new_name)
        if filename != new_name:
            to_rename.append((filename, new_name))
            if multi_disc and not single_folder:
                folders_to_create.add(os.path.join(path, f"{md_word}{disc_number:02d}"))

    if to_rename:
        print_filenames(to_rename)
        renames, clashes = plan_renames(
            [(os.path.join(path, filename), os.path.join(path, new_name)) for filename, new_name in to_rename]
        )
        if clashes:
            click.secho("\nNot renaming: these files would overwrite another file.", fg="red")
            for name in clashes:
                click.secho(f"   {os.path.relpath(name, path)}", fg="red")
            return
        if (
            auto_rename
            or await interaction.assume_defaults()
            or await interaction.confirm(
                click.style("\nWould you like to rename the files?", fg="magenta"),
                default=True,
            )
        ):
            for folder in folders_to_create:
                if not os.path.isdir(folder):
                    os.mkdir(folder)
            directory_move_pairs = set()
            for filename, new_name in to_rename:
                old_dir = os.path.dirname(os.path.join(path, filename))
                new_dir = os.path.dirname(os.path.join(path, new_name))

                if old_dir != path:
                    directory_move_pairs.add((os.path.splitext(filename)[1], old_dir, new_dir))
            _rename_or_undo(path, renames)

            # Update spectral_ids with new filenames, if spectrals were generated
            if spectral_ids:
                new_names = dict(to_rename)
                for key, value in spectral_ids.items():
                    if value in new_names:
                        spectral_ids[key] = new_names[value]

            # A folder holding one disc's tracks has its other files named for that disc (log.2.log)
            disc_of_folder = {folder: discs.pop() for folder, discs in folder_discs.items() if len(discs) == 1}
            move_non_audio_files(directory_move_pairs, disc_of_folder)
            delete_empty_folders(path)
    else:
        click.secho("\nNo file renaming is recommended.", fg="green")


def _rename_or_undo(path: str, renames: list[tuple[str, str]]) -> None:
    """Rename each pair, then the conversion record; put the renames back if one fails, the record can't, or a stop.

    Raises:
        UploadError: A rename failed, or the conversion record could not be updated.
    """
    # Local: the converter package imports the tagger.
    from salmon.converter.conversions import note_renamed

    done: list[tuple[str, str]] = []
    try:
        # One at a time, so an interrupt knows what to put back too.
        for rename in renames:
            rename_all([rename])
            done.append(rename)
        # Once, after the last: a record that cannot be written leaves every rename to put back, and itself as it was.
        note_renamed(path, renames)
    except BaseException as e:
        undone = _undo_renames(path, done)
        if isinstance(e, RenameStopped):
            stopped = RenameStopped(e.error, done, e.failed, renames[len(done) + 1 :])
            raise UploadError(f"{stopped.report(path)}\n{undone}") from e
        if isinstance(e, OSError):
            raise UploadError(f"Could not update the record of how {path} was converted ({e}).\n{undone}") from e
        click.secho(undone, fg="red", bold=True)
        raise


def _undo_renames(path: str, done: list[tuple[str, str]]) -> str:
    """Put back the renames a stopped batch made, and say what names the album's files are left with."""
    # Local: the converter package imports the tagger.
    from salmon.converter.conversions import note_renamed

    try:
        rename_all([(new, old) for old, new in reversed(done)])
    except RenameStopped as e:
        message = f"Undoing them stopped too. {e.report(path)}\nThe album's files are only partly renamed."
        try:
            # The record follows the renames still in place, as the disk does.
            note_renamed(path, done[: len(done) - len(e.done)])
        except OSError as record_error:
            message += f"\nCould not update the record of how {path} was converted ({record_error})."
        return message
    return "Those renames were undone: the album's files keep their old names."


def print_filenames(to_rename):
    """Print all the proposed filename changes."""
    click.secho("\nProposed filename changes:", fg="yellow", bold=True)
    for filename, new_name in to_rename:
        click.echo(f"   {filename} {ARROWS} {new_name}")


def generate_file_name(tags, ext, multiple_artists, trackno_or=None, *, various=False):
    """Generate the template keys and format the template with the tags.

    ``various`` puts ``various_artist_word`` in {artist}.
    """
    template = cfg.upload.formatting.file_template
    keys = [fn for _, fn, _, _ in Formatter().parse(template) if fn]
    if (
        "artist" in keys
        and cfg.upload.formatting.no_artist_in_filename_if_only_one_album_artist
        and not multiple_artists
    ):
        keys.remove("artist")
        template = cfg.upload.formatting.one_album_artist_file_template
    if isinstance(tags, dict):
        template_keys: dict[str, str | int] = {}
        for k in keys:
            tag_val = tags.get(k)
            if tag_val is not None and isinstance(tag_val, list) and tag_val:
                template_keys[k] = _parse_integer(tag_val[0])
            else:
                template_keys[k] = _parse_integer("")
    else:
        template_keys = {}
        for k in keys:
            raw_val = getattr(tags, k, "")
            if k == "artist" and isinstance(raw_val, list) and raw_val:
                raw_val = raw_val[0]
            val = _parse_integer(raw_val if isinstance(raw_val, (str, int)) else str(raw_val))
            template_keys[k] = val

    # This counted "," and "&" in ARTIST, so "Earth, Wind & Fire" was two artists
    if "artist" in keys and various:
        template_keys["artist"] = cfg.upload.formatting.various_artist_word
    if "tracknumber" in keys and trackno_or is not None:
        template_keys["tracknumber"] = trackno_or
    new_base = template.format(**template_keys)
    if cfg.upload.formatting.fullwidth_replacements:
        for char, sub in BLACKLISTED_FULLWIDTH_REPLACEMENTS.items():
            new_base = new_base.replace(char, sub)
    new_base = re.sub(BLACKLISTED_CHARS, cfg.upload.formatting.blacklisted_substitution, new_base)
    # Cleaned before the extension is added, so a space left at the end of the name does not sit before the dot
    new_base = clean_name(new_base)
    if not has_visible_chars(new_base):
        raise UploadError(f"Not naming a file {new_base!r}{ext}: its tags leave nothing visible in file_template.")
    return new_base + ext


def _parse_integer(value):
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return f"{int(value):02d}"
    return value


def _get_tag_number(tracktags, field):
    """Read a disc/track number off a tag object or dict, defaulting to 1."""
    number = _parse_tag_number(tracktags, field)
    return 1 if number is None else number


def _parse_tag_number(tracktags, field):
    """The tag's number, or None when it is absent or not a number (unlike ``_get_tag_number``, no default)."""
    value = tracktags.get(field) if isinstance(tracktags, dict) else getattr(tracktags, field, None)

    if isinstance(value, list) and value:
        value = value[0]
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split("/")[0]
        # str.isdecimal(), not str.isdigit(): isdigit() accepts some Unicode digits (superscript
        # "2") that int() then rejects, while isdecimal() is true for exactly what int() accepts.
        return int(value) if value.isdecimal() else None
    if isinstance(value, int):
        return value
    return None


def _has_tag(tracktags, field):
    """Whether a tag object or dict carries a (possibly malformed) value for ``field``."""
    value = tracktags.get(field) if isinstance(tracktags, dict) else getattr(tracktags, field, None)
    return value is not None


def move_non_audio_files(directory_move_pairs, disc_of_folder=None):
    """
    Move the files other than the tracks (logs, cues, covers, scan folders) out of each folder the tracks
    were moved out of, into the tracks' new folder. A file never replaces one already there: it stays where
    it is, with a warning.

    When the tracks of a multi-disc release are moved into the release folder, ``disc_of_folder`` gives the
    disc number of each folder that held one disc: its files are named for that disc (``rip.log`` from disc 2
    becomes ``rip.2.log``, ``Scans`` becomes ``Scans.2``), so several discs' files can sit side by side.
    """
    track_dirs = [os.path.normpath(old_dir) for _ext, old_dir, _new_dir in directory_move_pairs]
    for ext, old_dir, new_dir in sorted(directory_move_pairs):
        if old_dir == new_dir or not os.path.isdir(old_dir):
            continue
        disc_number = (disc_of_folder or {}).get(old_dir)
        for file in sorted(os.listdir(old_dir)):
            old_path = os.path.join(old_dir, file)
            if file.endswith(ext) and not os.path.isdir(old_path):
                continue
            # A folder that held tracks too (Disc 1/Bonus) has its files moved on its own pass.
            if os.path.isdir(old_path) and _holds_any(old_path, track_dirs):
                continue
            new_file = file
            if disc_number is not None:
                base, file_ext = (file, "") if os.path.isdir(old_path) else os.path.splitext(file)
                new_file = f"{base}.{disc_number}{file_ext}"
            new_path = os.path.join(new_dir, new_file)
            if os.path.lexists(new_path):
                click.secho(f"Left {old_path} where it is: {new_path} already exists.", fg="yellow")
                continue
            shutil.move(old_path, new_path)


def _holds_any(folder: str, paths: list[str]) -> bool:
    folder = os.path.normpath(folder)
    return any(path == folder or path.startswith(folder + os.sep) for path in paths)


def delete_empty_folders(path):
    """Remove every folder under path left empty, deepest first so a parent its children emptied goes too.

    path itself is kept, and symlinks are neither followed nor removed.
    """
    for root, _dirs, _files in os.walk(path, topdown=False):
        if os.path.samefile(root, path) or os.path.islink(root):
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            continue
