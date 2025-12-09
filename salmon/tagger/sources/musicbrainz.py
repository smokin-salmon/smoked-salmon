import re
from collections import defaultdict

import musicbrainzngs

from salmon.errors import ScrapeError
from salmon.sources import MusicBrainzBase
from salmon.tagger.sources.base import MetadataMixin

RELEASE_TYPES = {
    "Album": "Album",
    "Single": "Single",
    "EP": "EP",
    "Compilation": "Compilation",
    "Soundtrack": "Soundtrack",
    "Interview": "Interview",
    "Live": "Live album",
    "Remix": "Remix",
    "DJ-mix": "DJ Mix",
    "Mixtape/Street": "Mixtape",
}


musicbrainzngs.set_useragent("salmon", "1.0", "noreply@salm.on")  # type: ignore[attr-defined]


class Scraper(MusicBrainzBase, MetadataMixin):
    def parse_release_title(self, soup):
        return soup["title"]

    def parse_cover_url(self, soup):
        if soup["cover-art-archive"] and soup["cover-art-archive"]["front"] == "true":
            try:
                r = musicbrainzngs.get_image_list(soup["id"])
            except musicbrainzngs.musicbrainz.ResponseError:  # type: ignore[attr-defined]
                return None

            for image in r["images"]:
                if image["approved"] and image["front"]:
                    return image["image"]
        return None

    def parse_release_year(self, soup):
        date = self.parse_release_date(soup)
        try:
            match = re.search(r"(\d{4})", date) if date else None
            return int(match[1]) if match else None
        except (TypeError, IndexError):
            return None

    def parse_release_date(self, soup):
        try:
            return soup["release-event-list"][0]["date"]
        except (KeyError, IndexError):
            return None

    def parse_release_group_year(self, soup) -> int | None:
        try:
            first_release_date = soup["release-group"]["first-release-date"]
            match = re.search(r"(\d{4})", first_release_date) if first_release_date else None
            return int(match[1]) if match else self.parse_release_year(soup)
        except (KeyError, IndexError, TypeError):
            return self.parse_release_year(soup)

    def parse_release_label(self, soup):
        try:
            return soup["label-info-list"][0]["label"]["name"]
        except (KeyError, IndexError):
            return None

    def parse_release_catno(self, soup):
        try:
            catno = soup["label-info-list"][0]["catalog-number"]
            if not catno:
                return None

            # Convert to string and clean whitespace
            catno = str(catno).strip()

            # Check for exact matches of none variants (case insensitive)
            none_variants = ["none", "[none]", "(none)", "n/a", "[n/a]", "(n/a)"]
            if catno.lower() in none_variants:
                return None

            # Don't match "non" or "NON" as they're often part of valid catalog numbers
            # Don't match if "none" is part of a longer string (e.g. "none33", "ONONE", "None Audio")
            return catno

        except (KeyError, IndexError):
            return None

    def parse_release_type(self, soup):
        try:
            return RELEASE_TYPES[soup["release-group"]["type"]]
        except KeyError:
            return None

    def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        for disc in soup["medium-list"]:
            for track in disc["track-list"]:
                try:
                    tracks[str(disc["position"])][str(track["number"])] = self.generate_track(
                        trackno=track["number"],
                        discno=disc["position"],
                        artists=parse_artists(track["recording"]["artist-credit"]),
                        title=track["recording"]["title"],
                    )
                except (ValueError, IndexError) as e:
                    raise ScrapeError("Could not parse tracks.") from e
        return dict(tracks)


def parse_artists(artist_credits):
    """
    Create the artists list from the given list of artists. MusicBrainz does
    some weird bullshit for guests, where it will separate the big list of
    artists with the string ' feat. ', after which point all of the artists are guests.
    """
    artists = []
    is_guest = False
    for artist in artist_credits:
        if artist == " feat. ":
            is_guest = True
        elif isinstance(artist, dict):
            artists.append((artist["artist"]["name"], "guest" if is_guest else "main"))
    return artists
