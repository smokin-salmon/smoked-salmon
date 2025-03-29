import re
from collections import defaultdict
from html import unescape

from salmon.common import RE_FEAT, parse_copyright, re_split
from salmon.errors import ScrapeError
from salmon.sources import TidalBase
from salmon.tagger.sources.base import MetadataMixin

ROLES = {
    "MAIN": "main",
    "FEATURED": "guest",
}

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
        if not soup["cover"]:
            return None
        return self.image_url.format(album_id=soup["cover"].replace("-", "/"))

    def parse_release_year(self, soup):
        try:
            return int(re.search(r"(\d{4})", soup["releaseDate"])[1])
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

    def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        for track in soup["tracklist"]:
            tracks[str(track["volumeNumber"])][
                str(track["trackNumber"])
            ] = self.generate_track(
                trackno=track["trackNumber"],
                discno=track["volumeNumber"],
                artists=self.parse_artists(
                    track["artists"], track["title"], track["id"]
                ),
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
            data["label"].lower() == a.lower() and i == "main"
            for a, i in data["artists"]
        ):
            return "Self-Released"
        return data["label"]

    def parse_artists(self, artists, title, track_id):  # noqa: C901
        """
        Iterate over all artists and roles, returning a compliant list of
        artist tuples.
        """
        result = []
        artist_set = set()

        feat = RE_FEAT.search(title)
        if feat:
            for artist in re_split(feat[1]):
                result.append((unescape(artist), "guest"))
                artist_set.add(unescape(artist).lower())

        remix_str = ""
        remixer_str = re.search(r" \((.*) [Rr]emix\)", title)
        if remixer_str:
            remix_str = unescape(remixer_str[1]).lower()

        all_guests = all(a["type"] == "FEATURED" for a in artists)
        for artist in artists:
            for a in re_split(artist["name"]):
                feat = RE_FEAT.search(a)
                if feat:
                    for artist_ in re_split(feat[1]):
                        result.append((unescape(artist_), "guest"))
                        artist_set.add(unescape(artist_).lower())
                    a = re.sub(feat[0] + "$", "", a).rstrip()
                if artist["type"] in ROLES and unescape(a).lower() not in artist_set:
                    if unescape(a).lower() in remix_str:
                        result.append((unescape(a), "remixer"))
                    elif all_guests:
                        result.append((unescape(a), "main"))
                    else:
                        result.append((unescape(a), ROLES[artist["type"]]))
                    artist_set.add(unescape(a).lower())

        if "mix" in title.lower():  # Get contributors for (re)mixes.
            attempts = 0
            while True:
                try:
                    artists = self.get_json_sync(
                        f"/tracks/{track_id}/contributors",
                        params={"countryCode": self.country_code, "limit": 25},
                    )["items"]
                    break
                except ScrapeError:
                    attempts += 1
                    if attempts > 3:
                        break
            for artist in artists:
                if (
                    artist["role"] == "Remixer"
                    and artist["name"].lower() not in artist_set
                ):
                    result.append((unescape(artist["name"]), "remixer"))
                    artist_set.add(artist["name"].lower())

        # In case something is fucked, have a failsafe of returning all artists.
        return result if result else [(unescape(a["name"]), "main") for a in artists]
