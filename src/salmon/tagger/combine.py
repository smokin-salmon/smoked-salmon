import contextlib
import re
from collections import defaultdict
from itertools import chain

import asyncclick as click
from unidecode import unidecode

from salmon.common import re_strip
from salmon.common.strings import comparable
from salmon.errors import TrackCombineError
from salmon.tagger.pairing import pair_by_content
from salmon.tagger.sources import METASOURCES
from salmon.tagger.sources.base import determine_label_type, generate_artists, standardize_genres

PREFERENCES = [
    "Tidal",
    "Deezer",
    "Qobuz",
    "Bandcamp",
    "MusicBrainz",
    "Discogs",
    "Beatport",
    "Apple Music",
]


def get_source_from_link(url):
    if not url:
        return None
    for name, source in METASOURCES.items():
        if source.Scraper.regex.match(url):
            return name


def combine_metadatas(*metadatas, base=None, source_url=None):
    """
    This function takes a bunch of chosen metadata and splices
    together values to form one unified metadata dictionary.
    It runs through them in the order of the sources specified in
    the PREFERENCES list. Nonexistent data is replaced by existing
    data, and some are combined, like release comments. Due to this,
    it's fairly important that the base metadata contain the correct
    number of tracks.
    """
    url_sources: set[str] = set()
    if base and base.get("url", False):
        link_source = get_source_from_link(base["url"])
        if link_source:
            url_sources.add(link_source)

    sources = sort_metadatas(metadatas)

    source = get_source_from_link(source_url)
    ordered_preferences = ([source] if source in PREFERENCES else []) + [p for p in PREFERENCES if p != source]

    from_preferred_source = True

    for pref in ordered_preferences:
        for metadata in sources[pref]:
            if base is None:
                base = metadata
                if base.get("url", False):
                    link_source = get_source_from_link(base["url"])
                    if link_source:
                        url_sources.add(link_source)
                from_preferred_source = False
                continue

            base["genres"] += metadata["genres"]

            # The first selected metadata source should be able to replace stale
            # embedded/local title data. Otherwise WEB uploads can preserve a
            # composite ALBUM tag from the source files even after selecting a
            # clean scraped release title.
            if from_preferred_source:
                if metadata["title"]:
                    base["title"] = metadata["title"]
                if metadata["edition_title"] is not None:
                    base["edition_title"] = metadata["edition_title"]

            with contextlib.suppress(TrackCombineError):
                base["tracks"] = combine_tracks(base["tracks"], metadata["tracks"], from_preferred_source)

            if (
                (not base["catno"] or not base["label"])
                and metadata["label"]
                and metadata["catno"]
                and (not base["label"] or any(w in metadata["label"] for w in base["label"].split()))
            ) and (base["source"] != "WEB" or (base["source"] == "WEB" and from_preferred_source)):
                base["label"] = metadata["label"]
                base["catno"] = metadata["catno"]

            if not base["label"] and metadata["label"]:
                base["label"] = metadata["label"]
                if not base["catno"] and metadata["catno"]:
                    base["catno"] = metadata["catno"]

            if metadata["comment"]:
                if not base["comment"]:
                    base["comment"] = metadata["comment"]
                else:
                    base["comment"] += f"\n\n{'-' * 32}\n\n" + metadata["comment"]

            if not base["cover"]:
                base["cover"] = metadata["cover"]
            if not base["edition_title"]:
                base["edition_title"] = metadata["edition_title"]
            if not base["year"]:
                base["year"] = metadata["year"]
            if not base["group_year"] or (
                str(metadata["group_year"]).isdigit() and int(metadata["group_year"]) < int(base["group_year"])
            ):
                base["group_year"] = metadata["group_year"]
            if not base["date"] and metadata["date"]:
                base["date"] = metadata["date"]
                if metadata["year"]:
                    base["year"] = metadata["year"]
                if metadata["group_year"]:
                    base["group_year"] = metadata["group_year"]
            if not base["rls_type"] or base["rls_type"] == "Album":
                base["rls_type"] = metadata["rls_type"]
            if not base["upc"]:
                base["upc"] = metadata["upc"]

            from_preferred_source = False

        if sources[pref] and base is not None:
            # Process URLs from all metadata entries from this source
            for metadata in sources[pref]:
                if "url" in metadata:
                    link_source = get_source_from_link(metadata["url"])
                    if link_source and metadata["url"] not in base["urls"]:
                        base["urls"].append(metadata["url"])
                        url_sources.add(link_source)

    if base is None:
        raise ValueError("No metadata provided to combine")

    assert base is not None  # Type narrowing for pyright

    if not base["group_year"]:
        base["group_year"] = base["year"]

    if "url" in base:
        del base["url"]

    for disc in base["tracks"].values():
        for track in disc.values():
            track.pop("artist_variations", None)

    base["artists"], base["tracks"] = generate_artists(base["tracks"])
    base["genres"] = standardize_genres(base["genres"])
    base["label"] = determine_label_type(base["label"], base["artists"])
    return base


def sort_metadatas(metadatas):
    """Split the metadatas by source."""
    sources = defaultdict(list)
    for source, md in metadatas:
        sources[source].append(md)
    return sources


def _extract_remixers_from_title(title):
    # Mix types that don't indicate a remixer when alone
    common_mix_types = {
        "original",
        "extended",
        "radio",
        "club",
        "instrumental",
        "acoustic",
        "album",
        "vocal",
        "main",
        "dub",
        "edit",
    }

    # Match patterns like (Remixer Remix), (Remixer Mix), (Remixer Radio Mix), etc.
    # Also matches compound mix types like "Vocal Mix", "Club Mix", etc.
    match = re.search(r"\(([^()]*?)\s+(?:Club|Radio|Vocal|Dub|Extended)?\s*(?:Remix|Mix|Edit)\)", title, re.IGNORECASE)
    if match:
        remixers = match.group(1).strip()
        # Split on common delimiters and strip each name
        remixer_list = [r.strip() for r in re.split(r"\s*(?:&|,|/|;|\+)\s*", remixers)]
        # Filter out common mix types and return remaining as remixers
        return [(r, "remixer") for r in remixer_list if r.lower() not in common_mix_types]
    return []


def _natural_track_key(value):
    """Sort keys numerically while still handling mixed values like A1/B2."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower()) for part in re.split(r"(\d+)", str(value)) if part
    )


def _sorted_naturally(items):
    return sorted(items, key=lambda item: _natural_track_key(item[0]))


def _pair_base_tracks(btracks, mtracks):
    """Order the local track dicts to line up with the scraped tracklist.

    Returns a list of length ``len(mtracks)`` where entry ``i`` is the local
    track belonging to ``mtracks[i]``, or ``None`` if no local track is left for
    it (the caller raises TrackCombineError for those), and how each was paired.
    Local tracks beyond the scraped tracklist are left unclaimed and untouched.

    Tracks numbered by their files' own TRACKNUMBER tags keep their numbers unless
    an ISRC or an exact title says otherwise. A track dict without the "numbered"
    key (another source's) counts as numbered.
    """
    numbered = all(track.get("numbered", True) for track in btracks)
    pairing, reasons = pair_by_content(btracks, mtracks, numbered=numbered)
    return [None if bi is None else btracks[bi] for bi in pairing], reasons


def _numbers_usable(meta):
    """True when, on every disc of the source, the track numbers are exactly 1..n (n = the disc's track count),
    read as decimal integers. One disc that is not makes the whole source unusable for numbers (vinyl sides on
    Discogs come as `A1`, `S` or `5`)."""
    for disc in meta.values():
        nums = []
        for num in disc:
            text = str(num).strip()
            if not (text.isascii() and text.isdecimal()):
                return False
            nums.append(int(text))
        if sorted(nums) != list(range(1, len(nums) + 1)):
            return False
    return True


def _new_track_numbers(mtracks, ordered, usable, update_track_numbers):
    """The new track number of each paired local track (None where there is no local track), in the order of
    `mtracks`. Used by both the merge and the check that it loses no track, so the two never disagree.

    A usable source: its number when `update_track_numbers`, else the file's own. An unusable source: a file
    numbered by its own TRACKNUMBER tag keeps it, and any other file gets its place (1..n) among the paired
    tracks of its disc, in the source's order."""
    seen: dict[str, int] = {}
    result = []
    for mtrack, btrack in zip(mtracks, ordered, strict=True):
        if btrack is None:
            result.append(None)
            continue
        seen[btrack["disc#"]] = place = seen.get(btrack["disc#"], 0) + 1
        if usable:
            new_num = mtrack["track#"] if (update_track_numbers and mtrack["track#"]) else btrack["track#"]
        elif btrack.get("numbered", True):
            new_num = btrack["track#"]
        else:
            new_num = str(place)
        result.append(new_num)
    return result


def _refuse_renumbering_that_loses_a_track(btracks, mtracks, ordered, usable, update_track_numbers):
    """Raise TrackCombineError, before anything is merged, if two local tracks would end up in one slot, so one
    would be overwritten and lost: a paired track taking the number of a track the source has no track for, or
    two paired tracks taking the same number on one disc (a track keeps its own disc when it is renumbered).
    Pairing by content can pair a local track anywhere in the folder, not only at its own rank."""
    new_numbers = _new_track_numbers(mtracks, ordered, usable, update_track_numbers)
    paired = {id(btrack) for btrack in ordered if btrack is not None}
    taken = {(btrack["disc#"], btrack["track#"]) for btrack in btracks if id(btrack) not in paired}
    for btrack, new_num in zip(ordered, new_numbers, strict=True):
        if btrack is None:
            continue  # The merge raises for a scraped track without a local one.
        slot = (btrack["disc#"], new_num)
        if slot in taken:
            raise TrackCombineError(f"Disc {slot[0]} track {slot[1]} would hold two local tracks, losing one.")
        taken.add(slot)


def _print_pairing(mtracks, ordered, reasons):
    """Show how local files were matched, so a bad pairing is visible before tagging."""
    if not any(r and r != "position" for r in reasons):
        return  # Paired by position only, as before: nothing worth reporting.
    click.secho("\nMatched local tracks to the metadata source:", fg="yellow", bold=True)
    for mtrack, btrack, why in zip(mtracks, ordered, reasons, strict=False):
        if btrack is None:
            click.secho(f"  {mtrack['track#']:>3}. {mtrack['title']} -> (no local track)", fg="red")
            continue
        moved = str(btrack["track#"]) != str(mtrack["track#"])
        click.secho(
            f"  {mtrack['track#']:>3}. {mtrack['title']} <- {btrack['title']} "
            f"(was track {btrack['track#']}, matched on {why})",
            fg="red" if why == "position" else ("yellow" if moved else "green"),
        )


def combine_tracks(base, meta, update_track_numbers):
    """Combine the metadata for the tracks of two different sources."""
    # Track dictionaries reflect discovery order, so sort discs and tracks naturally before pairing.
    btracks_sorted = [
        [track for _, track in _sorted_naturally(disc.items())] for _, disc in _sorted_naturally(base.items())
    ]
    btracks_flat = list(chain.from_iterable(btracks_sorted))

    # Pair on content instead of on sorted rank, so unnumbered local files don't
    # have to already be in the source's real tracklist order.
    mtracks_flat = [
        track for _, tracks in _sorted_naturally(meta.items()) for _, track in _sorted_naturally(tracks.items())
    ]
    ordered_btracks, pair_reasons = _pair_base_tracks(btracks_flat, mtracks_flat)
    usable = _numbers_usable(meta)
    new_numbers = iter(_new_track_numbers(mtracks_flat, ordered_btracks, usable, update_track_numbers))
    _refuse_renumbering_that_loses_a_track(btracks_flat, mtracks_flat, ordered_btracks, usable, update_track_numbers)
    _print_pairing(mtracks_flat, ordered_btracks, pair_reasons)

    # If the source already provides remixer credits, skip title-based inference
    # to avoid adding spurious artists from mix names like "Attitude Dub Mix".
    meta_has_remixer = any(
        role == "remixer" for tracks in meta.values() for track in tracks.values() for _, role in track["artists"]
    )

    # Track numbers are applied after the merge loop; renumbering in place can
    # overwrite a base track that hasn't been visited yet once pairing reorders.
    renumbered = []

    # One entry per scraped track, in the order this loop visits them.
    paired_btracks = iter(ordered_btracks)
    for disc, tracks in _sorted_naturally(meta.items()):
        for num, track in _sorted_naturally(tracks.items()):
            btrack = next(paired_btracks)
            if btrack is None:
                raise TrackCombineError(f"Disc {disc} track {num} does not exist.")

            if (
                # Use unidecode comparison when there are accents in the title
                re_strip(unidecode(track["title"])) != re_strip(unidecode(btrack["title"]))
                and btrack["title"] is not None
            ) and (
                btrack["title"]
                and track["title"]
                # Allow replacement if the base title is part of the meta title (e.g., remix scenario)
                and re_strip(unidecode(btrack["title"])) in re_strip(unidecode(track["title"]))
            ):
                btrack["title"] = track["title"]

            if btrack["title"] is None:
                btrack["title"] = track["title"]

            # Scraped title is the same than title when ignoring metadatas, and it contains accents and special
            # characters, prefer that one.
            if re_strip(track["title"]) != re_strip(unidecode(track["title"])) and re_strip(
                unidecode(track["title"])
            ) == re_strip(unidecode(btrack["title"])):
                btrack["title"] = track["title"]

            base_artists = {(comparable(a[0]), a[1]) for a in btrack["artists"]}
            btrack["artists"] = list(btrack["artists"])
            variations = {(v["name"], v["role"]): v["anv"] for v in track.get("artist_variations", [])}
            for a in track["artists"]:
                if (comparable(a[0]), a[1]) in base_artists:
                    continue
                # The tags may spell the artist the way the release credits them (Discogs ANV)
                anv = variations.get(a)
                if anv and (comparable(anv), a[1]) in base_artists:
                    continue
                btrack["artists"].append(a)

            # Skip title-based remixer inference if remixers are already credited
            # (e.g. from Discogs), to avoid adding spurious artists from mix names
            if not meta_has_remixer:
                remixers = _extract_remixers_from_title(track["title"])
                for remixer in remixers:
                    if (comparable(remixer[0]), remixer[1]) not in base_artists:
                        btrack["artists"].append(remixer)

            btrack["artists"] = check_for_artist_fragments(btrack["artists"])

            if track["explicit"]:
                btrack["explicit"] = True
            if not btrack["format"]:
                btrack["format"] = track["format"]
            if not btrack["isrc"]:
                btrack["isrc"] = track["isrc"]
            if not btrack["replay_gain"]:
                btrack["replay_gain"] = track["replay_gain"]
                btrack["title"] = track["title"]
            # A source whose numbering is not 1..n on each disc has no totals to give either.
            if usable and not btrack["tracktotal"]:
                btrack["tracktotal"] = track["tracktotal"]
            if usable and not btrack["disctotal"]:
                btrack["disctotal"] = track["disctotal"]
            new_num = next(new_numbers)
            renumbered.append((btrack, btrack["disc#"], btrack["track#"], new_num))

    # Clear every old slot before filling the new ones, so two tracks swapping
    # positions doesn't leave one of them deleted.
    for _, disc_no, old_num, _ in renumbered:
        base[disc_no].pop(old_num, None)
    for btrack, disc_no, _, new_num in renumbered:
        btrack["track#"] = new_num
        base[disc_no][new_num] = btrack
    return base


def check_for_artist_fragments(artists):
    """Check for artists that may be a fragment of another artist in the release."""
    names = [_comparable_words(a) for a, _ in artists]
    for a, i in artists.copy():
        words = _comparable_words(a)
        if len("".join(words)) <= 1:
            continue
        for other in names:
            if words != other and _is_word_run(words, other) and (a, i) in artists:
                artists.remove((a, i))
    return artists


def _comparable_words(name):
    """The words of a name with case, accents and punctuation dropped (`X-Men` is `xmen`)."""
    return [word for word in (comparable(w) for w in name.split()) if word]


def _is_word_run(words, other):
    """True when `words` is a contiguous run of whole words inside `other`."""
    return any(other[i : i + len(words)] == words for i in range(len(other) - len(words) + 1))
