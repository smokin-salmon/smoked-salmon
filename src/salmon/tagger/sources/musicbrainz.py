import re
from collections import defaultdict
from typing import Any

import asyncclick as click

from salmon.common import RE_FEAT_JOIN
from salmon.errors import ScrapeError
from salmon.sources import MusicBrainzBase, is_placeholder_catno
from salmon.sources.musicbrainz import NotFoundError
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


# The key fetch_data keeps the release's front cover under, fetched there so the synchronous parse asks nothing.
FRONT_COVER = "salmon-front-cover"


# The secondary types MusicBrainz's XML serializer gave in place of Album, the first the group has in this order.
_ALBUM_FALLBACK_TYPES = ("Compilation", "Remix", "Soundtrack", "Live", "Spokenword", "Interview")


def release_group_type(group: dict[str, Any]) -> str | None:
    """The release group's type as MusicBrainz's XML gave it, in its one `type` attribute."""
    primary = group.get("primary-type")
    secondaries = group.get("secondary-types") or []
    if primary == "Album":
        return next((kind for kind in _ALBUM_FALLBACK_TYPES if kind in secondaries), primary)
    if primary:
        return primary
    return secondaries[0] if secondaries else None


class Scraper(MusicBrainzBase, MetadataMixin):
    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
        release = await super().fetch_data(url, params, headers, follow_redirects, rls_id)
        release[FRONT_COVER] = await self.front_cover(release)
        return release

    async def front_cover(self, release: dict[str, Any]) -> str | None:
        """The release's approved front image in the Cover Art Archive; None, and no request, if it has none."""
        if not (release.get("cover-art-archive") or {}).get("front"):
            return None
        try:
            images = await self.fetch_cover_art(release["id"])
        except NotFoundError:
            return None
        except ScrapeError:
            # No cover rather than no release.
            click.secho("Cover Art Archive request failed: no cover.", fg="yellow")
            return None
        for image in images["images"]:
            if image["approved"] and image["front"]:
                return image["image"]
        return None

    def parse_release_title(self, soup):
        return soup["title"]

    def parse_cover_url(self, soup):
        return soup.get(FRONT_COVER)

    def parse_release_year(self, soup):
        date = self.parse_release_date(soup)
        try:
            match = re.search(r"(\d{4})", date) if date else None
            return int(match[1]) if match else None
        except (TypeError, IndexError):
            return None

    def parse_release_date(self, soup):
        # The JSON gives "" for an event with no date, where the XML had no date.
        try:
            return soup["release-events"][0]["date"] or None
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
            return soup["label-info"][0]["label"]["name"]
        except (KeyError, IndexError, TypeError):  # TypeError: a label-info with a null label
            return None

    def parse_release_catno(self, soup):
        try:
            catno = soup["label-info"][0]["catalog-number"]
            if not catno:
                return None

            # Convert to string and clean whitespace
            catno = str(catno).strip()

            # Exact matches of none variants only (case insensitive)
            if is_placeholder_catno(catno):
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
            kind = release_group_type(soup["release-group"])
        except KeyError:
            return None
        return RELEASE_TYPES.get(kind) if kind else None

    async def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        for disc in soup["media"]:
            # MusicBrainz leaves out the tracks of a disc with data tracks only.
            for track in disc.get("tracks", []):
                try:
                    artists = parse_artists(track["recording"]["artist-credit"])
                    credited_remixers = []
                    for relation in track["recording"].get("relations", []):
                        if relation["type"] == "remixer" and relation["target-type"] == "artist":
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


def parse_artists(artist_credits: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """
    Create the artists list from a MusicBrainz artist credit. Each credit carries the join
    phrase to the next; after a feat. one (" feat. ", " ft. ", " featuring "), all of the
    artists are guests, as for a Discogs join.
    """
    artists = []
    is_guest = False
    for credit in artist_credits:
        artists.append((credit["artist"]["name"], "guest" if is_guest else "main"))
        if RE_FEAT_JOIN.fullmatch((credit.get("joinphrase") or "").strip()):
            is_guest = True
    return artists
