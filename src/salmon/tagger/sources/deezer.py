import re
from collections import defaultdict
from html import unescape

from salmon.common import RE_FEAT, parse_copyright, re_split
from salmon.common.strings import guests_from_title
from salmon.sources import DeezerBase
from salmon.tagger.sources.base import MetadataMixin, label_names_a_main_artist

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
        # Not the base rule: "Not On Label" stays as read, as the merge can replace it with another source's label and
        # catalogue number, where it wouldn't replace "Self-Released". combine.py applies the base rule afterwards.
        if isinstance(data["label"], str) and label_names_a_main_artist(data["label"], data["artists"]):
            return "Self-Released"
        return data["label"]

    def parse_artists(self, artists, default_artists, title):
        """
        Iterate over all artists and roles, returning a compliant list of
        artist tuples.
        """
        result = []
        # Each contributor and album artist is one Deezer artist, so its name stays whole ("Earth, Wind & Fire").
        if artists:
            mains = [name.strip() for name in artists.get("mainartist", []) + artists.get("main_artist", [])]
            featured = [name.strip() for name in artists.get("featuredartist", []) + artists.get("featuring", [])]
        else:
            mains, featured = [artist["ART_NAME"].strip() for artist in default_artists], []

        # The title's featured credit lists several names in one string, so it is split. When it names every part
        # of a name a contributor gives whole ("feat. Earth, Wind & Fire"), those parts are that name, not guests.
        guests = guests_from_title(title)
        covered = set()
        for name in mains + featured:
            parts = [unescape(part) for part in re_split(name)]
            if len(parts) > 1 and all(part in guests for part in parts):
                covered.update(parts)
        result += [(guest, "guest") for guest in guests if guest not in covered]

        for a in mains:
            if a and (a, "main") not in result:
                result.append((a, "main"))
        for a in featured:
            if a and (a, "guest") not in result:
                result.append((a, "guest"))

        return result
