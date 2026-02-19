import re
from collections import defaultdict
from html import unescape

from salmon.common import RE_FEAT, parse_copyright, re_split
from salmon.sources import DeezerBase
from salmon.tagger.sources.base import MetadataMixin

RECORD_TYPES = {
    "album": "Album",
    "ep": "EP",
    "single": "Single",
}


class Scraper(DeezerBase, MetadataMixin):
    def parse_release_title(self, soup):
        return RE_FEAT.sub("", soup["title"])

    def parse_cover_url(self, soup):
        return soup["cover_xl"]

    def parse_release_year(self, soup):
        try:
            match = re.search(r"(\d{4})", soup["release_date"])
            return int(match[1]) if match else None
        except TypeError:
            return None
            # raise ScrapeError('Could not parse release year.') from e

    def parse_release_date(self, soup):
        return soup["release_date"]

    def parse_release_label(self, soup):
        return parse_copyright(soup["label"])

    def parse_genres(self, soup):
        return {g["name"] for g in soup["genres"]["data"]}

    def parse_release_type(self, soup):
        try:
            return RECORD_TYPES[soup["record_type"]]
        except KeyError:
            return None

    def parse_upc(self, soup):
        return soup["upc"]

    async def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        for track in soup["tracklist"]:
            tracks[str(track["DISK_NUMBER"])][str(track["TRACK_NUMBER"])] = self.generate_track(
                trackno=track["TRACK_NUMBER"],
                discno=track["DISK_NUMBER"],
                artists=self.parse_artists(track["SNG_CONTRIBUTORS"], track["ARTISTS"], track["SNG_TITLE"]),
                title=self.parse_title(track["SNG_TITLE"], track.get("VERSION", None)),
                isrc=track["ISRC"],
                explicit=track["EXPLICIT_LYRICS"],
                stream_id=track["SNG_ID"],
                md5_origin=track.get("MD5_ORIGIN"),
                media_version=track.get("MEDIA_VERSION"),
                lossless=True,
                mp3_320=True,
            )
        return dict(tracks)

    def process_label(self, data):
        if isinstance(data["label"], str) and any(
            data["label"].lower().startswith(a.lower()) and i == "main" for a, i in data["artists"]
        ):
            return "Self-Released"
        return data["label"]

    def parse_artists(self, artists, default_artists, title):
        """
        Iterate over all artists and roles, returning a compliant list of
        artist tuples.
        """
        result = []

        feat = RE_FEAT.search(title)
        if feat:
            for artist in re_split(feat[1]):
                result.append((unescape(artist), "guest"))

        if artists:
            for a in artists.get("mainartist", []) + artists.get("main_artist", []):
                for b in re_split(a):
                    if (b, "main") not in result:
                        result.append((b, "main"))
            for a in artists.get("featuredartist", []) + artists.get("featuring", []):
                for b in re_split(a):
                    if (b, "guest") not in result:
                        result.append((b, "guest"))
        else:
            for artist in default_artists:
                for b in re_split(artist["ART_NAME"]):
                    if (b, "main") not in result:
                        result.append((b, "main"))

        return result
