import re
from collections import defaultdict
from collections.abc import Collection

import asyncclick as click
import musicbrainzngs
from musicbrainzngs import musicbrainz as mb_module

from salmon.common import RE_FEAT_JOIN
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


musicbrainzngs.set_useragent("salmon", "1.0", "noreply@salm.on")

# The official special purpose artists, and the subsets of [unknown] that have one, as MusicBrainz lists them:
# https://musicbrainz.org/doc/Style/Unknown_and_untitled/Special_purpose_artist
SPECIAL_PURPOSE_ARTISTS = frozenset(
    {
        "f731ccc4-e22a-43af-a747-64213329e088",  # [anonymous]
        "33cf029c-63b0-41a0-9855-be2a3665fb3b",  # [data]
        "314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc",  # [dialogue]
        "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",  # [no artist]
        "9be7f096-97ec-4615-8957-8d40b5dcbc41",  # [traditional]
        "125ec42a-7229-4250-afc5-e057484327fe",  # [unknown]
        "89ad4ac3-39f7-470e-963a-56509c546377",  # Various Artists
        "7e84f845-ac16-41fe-9ff8-df12eb32af55",  # MusicBrainz Test Artist
        "66ea0139-149f-4a0c-8fbf-5ea9ec4a6e49",  # [Disney]
        "a0ef7e1d-44ff-4039-9435-7d5fefdeecc9",  # [theatre]
    }
)


class Scraper(MusicBrainzBase, MetadataMixin):
    def parse_release_title(self, soup):
        return soup["title"]

    def parse_cover_url(self, soup):
        if soup["cover-art-archive"] and soup["cover-art-archive"]["front"] == "true":
            try:
                r = musicbrainzngs.get_image_list(soup["id"])
            except mb_module.ResponseError:
                return None
            except (mb_module.WebServiceError, ValueError) as e:
                # No cover rather than no release. ValueError: a 200 whose body is not the JSON image list.
                click.secho(f"Cover Art Archive request failed ({type(e).__name__}): no cover.", fg="yellow")
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

    def parse_upc(self, soup):
        return soup.get("barcode") or None

    def parse_release_type(self, soup):
        try:
            return RELEASE_TYPES[soup["release-group"]["type"]]
        except KeyError:
            return None

    async def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        for disc in soup["medium-list"]:
            for track in disc["track-list"]:
                try:
                    recording = track["recording"]
                    artists = parse_artists(
                        recording["artist-credit"], track.get("artist-credit", []), composer_ids(recording)
                    )
                    credited_remixers = []
                    if "artist-relation-list" in track["recording"]:
                        for relation in track["recording"]["artist-relation-list"]:
                            if relation["type"] == "remixer":
                                credited_remixers.append(relation["artist"]["name"])
                                artists.append((relation["artist"]["name"], "remixer"))

                    tracks[str(disc["position"])][str(track["number"])] = self.generate_track(
                        trackno=track["number"],
                        discno=disc["position"],
                        artists=artists,
                        credited_remixers=credited_remixers,
                        title=track["title"] if "title" in track else track["recording"]["title"],
                    )
                except (ValueError, IndexError) as e:
                    raise ScrapeError("Could not parse tracks.") from e
        return dict(tracks)


def composer_ids(recording) -> set[str]:
    """The ids of the composers of the works a recording is a performance of."""
    return {
        relation["artist"]["id"]
        for performance in recording.get("work-relation-list", [])
        if performance["type"] == "performance"
        for relation in performance["work"].get("artist-relation-list", [])
        if relation["type"] == "composer"
    }


def _credited(artist_credits) -> list[tuple[str, str, str]]:
    """(id, name, role) of each artist in a credit. MusicBrainz separates the list with join phrases;
    after a feat. one (" feat. ", " ft. ", " featuring "), all of the artists are guests, as for a Discogs join.
    """
    credited = []
    is_guest = False
    for artist in artist_credits:
        if isinstance(artist, str) and RE_FEAT_JOIN.fullmatch(artist.strip()):
            is_guest = True
        elif isinstance(artist, dict):
            credited.append((artist["artist"]["id"], artist["artist"]["name"], "guest" if is_guest else "main"))
    return credited


def parse_artists(recording_credits, track_credits=(), composers: Collection[str] = ()):
    """
    The artists of a track: those the recording is credited to, then those the release adds on the track.
    An artist in both is one artist, with the track's name and the recording's role. One only on the track
    is left out if they composed the work (the track credit of classical releases is the composer, where
    the recording's is the performers), or is a special purpose artist such as "[dialogue]".
    """
    artists = {mbid: (name, role) for mbid, name, role in _credited(recording_credits)}
    for mbid, name, role in _credited(track_credits):
        if mbid in artists:
            artists[mbid] = (name, artists[mbid][1])
        elif mbid not in composers and mbid not in SPECIAL_PURPOSE_ARTISTS:
            artists[mbid] = (name, role)
    return list(artists.values())
