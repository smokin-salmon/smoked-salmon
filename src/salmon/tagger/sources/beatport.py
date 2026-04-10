import re
from collections import defaultdict

from salmon.errors import ScrapeError
from salmon.sources import BeatportBase
from salmon.tagger.sources.base import MetadataMixin

SPLIT_GENRES = {
    "Leftfield House & Techno": {"Leftfield House", "Techno"},
    "Melodic House & Techno": {"Melodic House", "Techno"},
    "Electronica / Downtempo": {"Electronic", "Downtempo"},
    "Funk / Soul / Disco": {"Funk", "Soul", "Disco"},
    "Trap / Future Bass": {"Trap", "Future Bass"},
    "Indie Dance / Nu Disco": {"Indie Dance", "Nu Disco"},
    "Hardcore / Hard Techno": {"Hard Techno"},
    "Funky / Groove / Jackin' House": {"Funky", "Groove", "Jackin' House"},
    "Hip-Hop / R&B": {"Hip-Hop", "Rhythm & Blues"},
    "Minimal / Deep Tech": {"Minimal", "Deep Tech"},
    "Garage / Bassline / Grime": {"Garage", "Bassline", "Grime"},
    "Reggae / Dancehall / Dub": {"Reggae", "Dancehall", "Dub"},
}


class Scraper(BeatportBase, MetadataMixin):
    def parse_release_title(self, soup):
        try:
            return soup["release"]["name"]
        except (KeyError, IndexError) as e:
            raise ScrapeError("Could not parse release title") from e

    def parse_cover_url(self, soup):
        try:
            return soup["release"]["image"]["uri"]
        except (KeyError, IndexError) as e:
            raise ScrapeError("Could not parse cover URL") from e

    def parse_genres(self, soup):
        genres = {"Electronic"}
        try:
            for track in soup["tracks"]:
                genre_name = track["genre"]["name"]
                try:
                    genres |= SPLIT_GENRES[genre_name]
                except KeyError:
                    genres.add(genre_name)
            return genres
        except (KeyError, IndexError) as e:
            raise ScrapeError("Could not parse genres") from e

    def parse_release_year(self, soup):
        date = self.parse_release_date(soup)
        try:
            match = re.search(r"(\d{4})", date) if date else None
            if not match:
                raise ScrapeError("Could not parse release year.")
            return int(match[1])
        except (TypeError, IndexError) as e:
            raise ScrapeError("Could not parse release year.") from e

    def parse_release_date(self, soup):
        try:
            return soup["release"]["new_release_date"]
        except (KeyError, IndexError) as e:
            raise ScrapeError("Could not parse release date") from e

    def parse_release_label(self, soup):
        try:
            return soup["release"]["label"]["name"]
        except (KeyError, IndexError) as e:
            raise ScrapeError("Could not parse release label") from e

    def parse_release_catno(self, soup):
        try:
            return soup["release"]["catalog_number"]
        except (KeyError, IndexError) as e:
            raise ScrapeError("Could not parse catalog number") from e

    def parse_comment(self, soup):
        return None

    def parse_upc(self, soup):
        try:
            return soup["release"]["upc"]
        except (KeyError, IndexError) as e:
            raise ScrapeError("Could not parse UPC") from e

    async def parse_tracks(self, soup):
        tracks = defaultdict(dict)
        cur_disc = 1
        try:
            track_list = sorted(soup["tracks"], key=lambda x: int(x["id"]) if x["id"] is not None else 0)
            for i, track in enumerate(track_list, 1):
                track_num = str(i)
                # Get artists and remixers
                artists = []
                for artist in track["artists"]:
                    for split in re.split(" & |; | / ", artist["name"]):
                        artists.append((split, "main"))
                for remixer in track["remixers"]:
                    for split in re.split(" & |; | / ", remixer["name"]):
                        artists.append((split, "remixer"))

                # Get title with mix name if not Original Mix
                title = track["name"]
                if track["mix_name"] and track["mix_name"] != "Original Mix":
                    title += f" ({track['mix_name']})"

                tracks[str(cur_disc)][track_num] = self.generate_track(
                    trackno=track_num,
                    discno=cur_disc,
                    artists=artists,
                    title=title,
                    streamable=track["is_available_for_streaming"],
                    isrc=track["isrc"],
                )
            return dict(tracks)
        except (KeyError, IndexError) as e:
            raise ScrapeError("Could not parse tracks") from e
