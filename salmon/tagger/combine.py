import contextlib
import re
from collections import defaultdict
from itertools import chain

from unidecode import unidecode

from salmon.common import re_strip
from salmon.errors import TrackCombineError
from salmon.tagger.sources import METASOURCES
from salmon.tagger.sources.base import determine_label_type, generate_artists, standardize_genres

PREFERENCES = [
    "Tidal",
    "Deezer",
    "Qobuz",
    "Bandcamp",
    "MusicBrainz",
    "Junodownload",
    "Discogs",
    "Beatport",
    "iTunes",  # scraping half-broken, might want to put it back higher when fixed
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
    url_sources: set[str | None] = set()
    if base and base.get("url", False):
        url_sources.add(get_source_from_link(base["url"]))

    sources = sort_metadatas(metadatas)

    source = get_source_from_link(source_url)
    ordered_preferences = ([source] if source in PREFERENCES else []) + [p for p in PREFERENCES if p != source]

    from_preferred_source = True

    for pref in ordered_preferences:
        for metadata in sources[pref]:
            if base is None:
                base = metadata
                if base.get("url", False):
                    url_sources.add(get_source_from_link(base["url"]))
                from_preferred_source = False
                continue

            base["genres"] += metadata["genres"]

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
            if not base["date"]:
                base["date"] = metadata["date"]
                base["year"] = metadata["year"]
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

    if "url" in base:
        del base["url"]

    base["artists"], base["tracks"] = generate_artists(base["tracks"])
    base["genres"] = standardize_genres(set(base["genres"]))
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
    match = re.search(r"\((.*?)\s+(?:Club|Radio|Vocal|Dub|Extended)?\s*(?:Remix|Mix|Edit)\)", title, re.IGNORECASE)
    if match:
        remixers = match.group(1).strip()
        # Split on common delimiters and strip each name
        remixer_list = [r.strip() for r in re.split(r"\s*(?:&|,|/|;|\+)\s*", remixers)]
        # Filter out common mix types and return remaining as remixers
        return [(r, "remixer") for r in remixer_list if r.lower() not in common_mix_types]
    return []


def combine_tracks(base, meta, update_track_numbers):
    """Combine the metadata for the tracks of two different sources."""
    btracks = iter(chain.from_iterable([list(d.values()) for d in base.values()]))
    for disc, tracks in meta.items():
        for num, track in tracks.items():
            try:
                btrack = next(btracks)
            except StopIteration:
                raise TrackCombineError(f"Disc {disc} track {num} does not exist.") from None

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

            base_artists = {(re_strip(a[0]), a[1]) for a in btrack["artists"]}
            btrack["artists"] = list(btrack["artists"])
            for a in track["artists"]:
                if (re_strip(a[0]), a[1]) not in base_artists:
                    btrack["artists"].append(a)
            remixers = _extract_remixers_from_title(track["title"])
            for remixer in remixers:
                if (re_strip(remixer[0]), remixer[1]) not in base_artists:
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
            if not btrack["tracktotal"]:
                btrack["tracktotal"] = track["tracktotal"]
            if not btrack["disctotal"]:
                btrack["disctotal"] = track["disctotal"]
            if update_track_numbers and track["track#"]:
                del base[btrack["disc#"]][btrack["track#"]]
                btrack["track#"] = track["track#"]
            base[btrack["disc#"]][btrack["track#"]] = btrack
    return base


def check_for_artist_fragments(artists):
    """Check for artists that may be a fragment of another artist in the release."""
    artist_set = {a for a, _ in artists}
    for a, i in artists.copy():
        for artist in artist_set:
            if a != artist and a in artist and len(a) > 1 and (a, i) in artists:
                artists.remove((a, i))
    return artists
