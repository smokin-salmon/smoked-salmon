import re
from collections import defaultdict
from html import unescape

from salmon.common import RE_FEAT, parse_copyright, re_split
from salmon.common.strings import guests_from_title, unbracketed
from salmon.sources import DeezerBase
from salmon.tagger.sources.base import MetadataMixin, add_track, label_names_a_main_artist

RECORD_TYPES = {
    "album": "Album",
    "ep": "EP",
    "single": "Single",
}


class Scraper(DeezerBase, MetadataMixin):
    states_credits = True

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
            artists, guessed = self.parse_artists(track["SNG_CONTRIBUTORS"], track["ARTISTS"], track["SNG_TITLE"])
            new_track = self.generate_track(
                trackno=track["TRACK_NUMBER"],
                discno=track["DISK_NUMBER"],
                artists=artists,
                credited_remixers=[name for name, role in artists if role == "remixer"],
                title=track["SNG_TITLE"],
                version=unbracketed(track.get("VERSION")),
                isrc=track["ISRC"],
                explicit=track["EXPLICIT_LYRICS"],
                stream_id=track["SNG_ID"],
                md5_origin=track.get("MD5_ORIGIN"),
                media_version=track.get("MEDIA_VERSION"),
                lossless=True,
                mp3_320=True,
            )
            new_track["assumed_artists"] = guessed
            add_track(tracks, str(track["DISK_NUMBER"]), str(track["TRACK_NUMBER"]), new_track)
        return dict(tracks)

    def process_label(self, data):
        # Not the base rule: "Not On Label" stays as read, as the merge can replace it with another source's label and
        # catalogue number, where it wouldn't replace "Self-Released". combine.py applies the base rule afterwards.
        if isinstance(data["label"], str) and label_names_a_main_artist(data["label"], data["artists"]):
            return "Self-Released"
        return data["label"]

    def parse_artists(self, artists, default_artists, title):
        """The track's credits as Deezer states them, then those salmon only guessed: the guests read from the title
        that Deezer doesn't state as featuring, and the track's ARTISTS given as mains when it has no contributors."""
        # Each contributor and track artist is one Deezer artist, so its name stays whole ("Earth, Wind & Fire").
        if artists:
            mains = [name.strip() for name in artists.get("mainartist", []) + artists.get("main_artist", [])]
            featured = [name.strip() for name in artists.get("featuredartist", []) + artists.get("featuring", [])]
            remixers = [name.strip() for name in artists.get("remixer", [])]
        else:
            mains, featured, remixers = [artist["ART_NAME"].strip() for artist in default_artists], [], []
        result = []
        for role, names in (("main", mains), ("guest", featured), ("remixer", remixers)):
            for name in names:
                if name and (name, role) not in result:
                    result.append((name, role))

        # The title's featured credit lists several names in one string, so it is split. When it names every part
        # of a name a contributor gives whole ("feat. Earth, Wind & Fire"), those parts are that name, not guests.
        # The rest come after what Deezer states: the merge keeps a name where it was first claimed.
        guests = list(dict.fromkeys(guests_from_title(title)))
        covered = set()
        for name in mains + featured:
            parts = [unescape(part) for part in re_split(name)]
            if len(parts) > 1 and all(part in guests for part in parts):
                covered.update(parts)
        guessed = [(guest, "guest") for guest in guests if guest not in covered and guest not in featured]
        if not artists:
            guessed = [(name, role) for name, role in result if role == "main"] + guessed
        return result + [credit for credit in guessed if credit not in result], guessed
