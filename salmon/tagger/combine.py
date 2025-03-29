from collections import defaultdict
from itertools import chain

from unidecode import unidecode

from salmon.common import re_strip
from salmon.errors import TrackCombineError
from salmon.tagger.sources import METASOURCES
from salmon.tagger.sources.base import generate_artists

PREFERENCES = [
    "Tidal",
    "Deezer",
    "Bandcamp",
    "MusicBrainz",
    "iTunes",
    "Junodownload",
    "Discogs",
    "Beatport",
    "Qobuz",
]


def get_source_from_link(url):
    for name, source in METASOURCES.items():
        if source.Scraper.regex.match(url):
            return name


def combine_metadatas(*metadatas, base=None):  # noqa: C901
    """
    This function takes a bunch of chosen metadata and splices
    together values to form one unified metadata dictionary.
    It runs through them in the order of the sources specified in
    the PREFERENCES list. Nonexistent data is replaced by existing
    data, and some are combined, like release comments. Due to this,
    it's fairly important that the base metadata contain the correct
    number of tracks.
    """
    url_sources = set()
    if base and base.get("url", False):
        url_sources.add(get_source_from_link(base["url"]))

    sources = sort_metadatas(metadatas)
    for pref in PREFERENCES:
        for metadata in sources[pref]:
            if not base:
                base = metadata
                if base.get("url", False):
                    url_sources.add(get_source_from_link(base["url"]))
                continue

            base["genres"] += metadata["genres"]

            try:
                base["tracks"] = combine_tracks(base["tracks"], metadata["tracks"])
            except TrackCombineError:
                pass

            if (
                (not base["catno"] or not base["label"])
                and metadata["label"]
                and metadata["catno"]
                and (
                    not base["label"]
                    or any(w in metadata["label"] for w in base["label"].split())
                )
            ):
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
                    base["comment"] += f'\n\n{"-"*32}\n\n' + metadata["comment"]

            if not base["cover"]:
                base["cover"] = metadata["cover"]
            if not base["edition_title"]:
                base["edition_title"] = metadata["edition_title"]
            if not base["year"]:
                base["year"] = metadata["year"]
            if not base["group_year"] or (
                str(metadata["group_year"]).isdigit()
                and int(metadata["group_year"]) < int(base["group_year"])
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

        if sources[pref] and "url" in sources[pref][0]:
            link_source = get_source_from_link(sources[pref][0]["url"])
            if link_source not in url_sources:
                base["urls"].append(sources[pref][0]["url"])
                url_sources.add(link_source)

    if "url" in base:
        del base["url"]

    base["artists"], base["tracks"] = generate_artists(base["tracks"])
    base["genres"] = list(set(base["genres"]))
    return base


def sort_metadatas(metadatas):
    """Split the metadatas by source."""
    sources = defaultdict(list)
    for source, md in metadatas:
        sources[source].append(md)
    return sources


def combine_tracks(base, meta):
    """Combine the metadata for the tracks of two different sources."""
    btracks = iter(chain.from_iterable([d.values() for d in base.values()]))
    for disc, tracks in meta.items():
        for num, track in tracks.items():
            try:
                btrack = next(btracks)
            except StopIteration:
                raise TrackCombineError(f"Disc {disc} track {num} does not exist.")
            # Use unidecode comparison when there are accents in the title
            if re_strip(unidecode(track["title"])) != re_strip(unidecode(btrack["title"])) and btrack["title"] is not None:
                continue
            if btrack["title"] is None:
                btrack["title"] = track["title"]
            # Scraped title is the same than title when ignoring metadatas, and it contains accents and special characters, prefer that one.
            if re_strip(track["title"]) != re_strip(unidecode(track["title"])) and re_strip(unidecode(track["title"])) == re_strip(unidecode(btrack["title"])):
                btrack["title"] = track["title"]
            base_artists = {(re_strip(a[0]), a[1]) for a in btrack["artists"]}
            btrack["artists"] = list(btrack["artists"])
            for a in track["artists"]:
                if (re_strip(a[0]), a[1]) not in base_artists:
                    btrack["artists"].append(a)
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
            if track["tracktotal"] and track["disctotal"]:
                btrack["tracktotal"] = track["tracktotal"]
                btrack["disctotal"] = track["disctotal"]
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
