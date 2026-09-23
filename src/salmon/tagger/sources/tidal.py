import re
import unicodedata
from collections import defaultdict
from html import unescape

from salmon.common import RE_FEAT, parse_copyright, re_split
from salmon.sources import TidalBase
from salmon.tagger.sources.base import MetadataMixin

RECORD_TYPES = {
    "ALBUM": "Album",
    "EP": "EP",
    "SINGLE": "Single",
}


class Scraper(TidalBase, MetadataMixin):
    regex = re.compile(r"^https?://.*(?:tidal|wimpmusic)\.com.*\/(album)\/([0-9]+)")

    def parse_release_title(self, soup):
        return RE_FEAT.sub("", soup["title"])

    def parse_cover_url(self, soup):
        return soup["cover"]

    def parse_release_year(self, soup):
        try:
            match = re.search(r"(\d{4})", soup["releaseDate"]) if soup.get("releaseDate") else None
            return int(match[1]) if match else None
        except TypeError:
            return None

    def parse_release_date(self, soup):
        date = soup["releaseDate"]
        if not date or date.endswith("01-01") and int(date[:4]) < 2013:
            return None
        return date

    def parse_release_type(self, soup):
        try:
            return RECORD_TYPES[soup["type"]]
        except KeyError:
            return None

    def parse_release_label(self, soup):
        return parse_copyright(soup["copyright"])

    def parse_upc(self, soup):
        return soup["upc"]

    async def parse_tracks(self, soup):
        album_artists = soup.get("artists") or []
        tracks = defaultdict(dict)
        for track in soup["tracklist"]:
            artists = self.parse_artists(track["artists"], track["title"], track["version"], album_artists)
            tracks[str(track["volumeNumber"])][str(track["trackNumber"])] = self.generate_track(
                trackno=track["trackNumber"],
                discno=track["volumeNumber"],
                artists=artists,
                title=self.parse_title(track["title"], track["version"]),
                replay_gain=track["replayGain"],
                peak=track["peak"],
                isrc=track["isrc"],
                explicit=track["explicit"],
                format_=track["audioQuality"],
                stream_id=track["id"],
                streamable=track["allowStreaming"],
            )
        return dict(tracks)

    def process_label(self, data):
        if isinstance(data["label"], str) and any(
            data["label"].lower().startswith(a.lower()) and i == "main" for a, i in data["artists"]
        ):
            return "Self-Released"
        return data["label"]

    def parse_artists(
        self,
        artists: list[dict],
        title: str,
        version: str | None,
        album_artists: list[dict],
    ) -> list[tuple[str, str]]:
        """Iterate over all artists and roles, returning a compliant list of artist tuples.

        Tidal's V2 API does not expose per-track artist roles (MAIN/FEATURED) or
        contributor credits, so roles are inferred from the album's main artists,
        the track title's ``feat.`` clauses, and the track version's remix info.

        Args:
            artists: List of track artist dictionaries from the Tidal API.
            title: Track title.
            version: Track version, e.g. ``"Tiësto Remix"``.
            album_artists: List of the album's main artist dictionaries.

        Returns:
            List of (artist_name, role) tuples.
        """
        result: list[tuple[str, str]] = []
        artist_set: set[str] = set()

        def norm(s: str) -> str:
            return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()

        album_artist_names = {norm(a["name"]) for a in album_artists}
        compilation = "various artists" in album_artist_names

        # Remixer names from a title "(X Remix)" clause and a version like "X Remix".
        remixers: set[str] = set()
        remixer_str = re.search(r" \(([^()]+) [Rr]emix\)$", title)
        if remixer_str:
            remixers.add(unescape(remixer_str[1]).strip())
        if version and re.search(r"[Rr]emix(es)?$", version):
            remixers.add(re.sub(r"[Rr]emix(es)?$", "", version).strip())
        remixer_names = {norm(r) for r in remixers}

        feat = RE_FEAT.search(title)
        if feat:
            for artist in re_split(feat[1]):
                result.append((unescape(artist), "guest"))
                artist_set.add(norm(unescape(artist)))

        for artist in artists:
            artist_without_feat = artist["name"]
            feat = RE_FEAT.search(artist["name"])
            if feat:
                for artist_ in re_split(feat[1]):
                    result.append((unescape(artist_), "guest"))
                    artist_set.add(norm(unescape(artist_)))
                artist_without_feat = re.sub(re.escape(feat[0]) + "$", "", artist_without_feat).rstrip()
            for a in re_split(artist_without_feat):
                name = unescape(a)
                if norm(name) in artist_set:
                    continue
                if norm(name) in remixer_names:
                    result.append((name, "remixer"))
                elif compilation or not album_artist_names or norm(name) in album_artist_names:
                    result.append((name, "main"))
                else:
                    result.append((name, "guest"))
                artist_set.add(norm(name))

        # The remixer may not be listed among the track artists; add them anyway.
        for remixer in remixers:
            if norm(remixer) not in artist_set:
                result.append((remixer, "remixer"))
                artist_set.add(norm(remixer))

        # In case something is fucked, have a failsafe of returning all artists.
        return result if result else [(unescape(a["name"]), "main") for a in artists]
