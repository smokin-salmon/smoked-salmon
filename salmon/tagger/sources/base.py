import re
from abc import ABC, abstractmethod
from collections import defaultdict
from copy import copy
from itertools import chain

from salmon import config
from salmon.common import fetch_genre, less_uppers, normalize_accents
from salmon.errors import GenreNotInWhitelist


class MetadataMixin(ABC):
    async def scrape_release_from_id(self, rls_id):
        """Run a scrape from the release ID."""
        return await self.scrape_release(self.format_url(rls_id=rls_id), rls_id=rls_id)

    async def scrape_release(self, url, rls_id=None):
        """
        Scrape the meatadata of a release and return a dictionary of scraped data.
        Data may vary depending on the source; unavailable keys will be left
        as None.
        """
        soup = await self.create_soup(url)
        data = {
            "title": self.parse_release_title(soup),
            "cover": self.parse_cover_url(soup),
            "genres": standardize_genres(
                [
                    g
                    for g in self.parse_genres(soup)
                    if g.lower() not in [c.lower() for c in config.BLACKLISTED_GENRES]
                ]
            ),
            "year": self.parse_release_year(soup),
            "group_year": self.parse_release_group_year(soup),
            "date": self.parse_release_date(soup),
            "edition_title": self.parse_edition_title(soup),
            "label": self.parse_release_label(soup),
            "catno": self.parse_release_catno(soup),
            "rls_type": self.parse_release_type(soup),
            "tracks": self.parse_tracks(soup),
            "upc": self.parse_upc(soup),
            "comment": self.parse_comment(soup),
            "scene": False,
            "encoding": None,
            "encoding_vbr": None,
            "media": None,
            "source": None,
            "url": url,
        }

        if rls_id:
            data["url"] = self.format_url(rls_id=rls_id, rls_name=data["title"])
        data["urls"] = [data["url"]]
        data["artists"], data["tracks"] = generate_artists(data["tracks"])
        data["tracks"] = append_remixers_to_track_titles(data["tracks"])
        data["tracks"] = assign_track_totals(data["tracks"])
        data["title"], data["rls_type"] = self.determine_rls_type(data)
        data["label"] = self.process_label(data)

        if data["catno"] and data["catno"].replace(" ", "") == str(data["upc"]):
            data["catno"] = None
        return data

    def generate_track(
        self,
        trackno,
        discno,
        artists,
        title,
        replay_gain=None,
        peak=None,
        format_=None,
        explicit=None,
        isrc=None,
        stream_id=None,
        streamable=None,
        **kwargs,
    ):
        """Return a generated track dictionary containing the required values."""
        return {
            "track#": str(trackno),
            "disc#": str(discno),
            "tracktotal": None,  # Filled out once all tracks are scraped.
            "disctotal": None,  # Same ^
            "artists": artists,
            "title": title,
            "replay_gain": replay_gain,
            "peak": peak,
            "explicit": explicit,
            "isrc": isrc,
            "format": format_,
            "stream_id": stream_id,
            "streamable": streamable,
            **kwargs,
        }

    def determine_rls_type(self, data):
        num_tracks = len(
            list(chain.from_iterable([d.values() for d in data["tracks"].values()]))
        )
        if re.search(r"\bE\.?P\.?\b", data["title"], re.IGNORECASE):
            return (
                re.sub(r"\bE\.?P\.?\b", "", data["title"], flags=re.IGNORECASE),
                "EP",
            )
        elif re.search(r"Single$", data["title"]):
            return (
                re.sub(r"-? *Single$", "", data["title"], flags=re.IGNORECASE),
                "Single",
            )
        elif re.search(r"original.*soundtrack", data["title"], flags=re.IGNORECASE):
            return data["title"], "Soundtrack"
        elif len([a for a in data["artists"] if a[1] == "main"]) > 6:
            return data["title"], "Compilation"
        elif num_tracks < 3:
            return data["title"], "Single"
        elif num_tracks < 6 or (num_tracks == 6 and not data["rls_type"]):
            return data["title"], "EP"
        if (data["rls_type"]):
            return data["title"], data["rls_type"]
        return data["title"], "Album"

    @abstractmethod
    def parse_release_title(self, soup):
        pass

    @abstractmethod
    def parse_release_year(self, soup):
        pass

    @abstractmethod
    def parse_release_label(self, soup):
        pass

    @abstractmethod
    def parse_tracks(self, soup):
        pass

    # The below parsers aren't present in every scraper.

    def parse_release_group_year(self, soup):
        return self.parse_release_year(soup)

    def parse_cover_url(self, soup):
        return

    def parse_release_date(self, soup):
        return

    def parse_edition_title(self, soup):
        return

    def parse_release_catno(self, soup):
        return

    def parse_release_type(self, soup):
        return

    def parse_genres(self, soup):
        return {}

    def parse_upc(self, soup):
        return

    def parse_comment(self, soup):
        return

    def process_label(self, data):
        def _compare(label, artist):
            label, artist = label.lower(), artist.lower()
            return label == artist or re.sub(r" music$", "", label) == artist

        label = data.get("label", "")

        if isinstance(label, str):
            # Check for "Not On Label" or "Self-Released" in the label
            if re.search(r"(not on label|self[- ]?released)", label, re.IGNORECASE):
                return "Self-Released"

            # Compare label to artist name
            if any(_compare(label, a) and i == "main" for a, i in data["artists"]):
                return "Self-Released"
        return data["label"]

    @staticmethod
    def parse_title(title, version):
        """
        Return a filtered title; all those parenthetical phrases belong
        in album info. We also filter out featured artists, since those are
        parsed with the artists.
        """
        if config.STRIP_USELESS_VERSIONS:
            base = re.sub(
                r" \(*(Original( Mix)?|Remastered|Clean|"
                r"Album.+edition|Album.+mix|feat[^\)]+)\)*$",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip()
            strip_set = {
                "original mix",
                "original",
                "remastered",
                "clean",
                "album edition",
                "album mix",
                title.lower(),
            }
        else:
            base = title.strip()
            strip_set = {title.lower()}

        if version:
            version = re.sub(r"[\(\)\[\]]", "", version)
            if version.lower() not in strip_set and version.lower() not in base.lower():
                base += f" ({version})"
        return base


def _generate_artist_pool_lower_case(tracks):
    artist_pool = {}
    for track in chain.from_iterable([d.values() for d in tracks.values()]):
        for name, _ in track["artists"]:
            strip_name = normalize_accents(name.lower())
            if strip_name not in artist_pool:
                artist_pool[strip_name] = name
            elif artist_pool[strip_name] != name:
                artist_pool[strip_name] = less_uppers(artist_pool[strip_name], name)
    return artist_pool


def generate_artists(tracks):
    """
    Generate a list of artist tuples from the artists of each individual
    track, then run all the artists through the filter/fixer that
    attempts to remedy bad splitting.
    """
    artist_pool = _generate_artist_pool_lower_case(tracks)
    artists = []
    for track in chain.from_iterable([d.values() for d in tracks.values()]):
        for name, import_ in track["artists"]:
            name = artist_pool[normalize_accents(name.lower())]
            if (name, import_) not in artists:
                artists.append((name, import_))
    artists, tracks = filter_artists(artists, tracks)
    return artists, tracks


def filter_artists(artists, tracks=None):
    """
    Takes a list of artist tuples, as [(artist, importance), ], and checks for
    badly split artists, such as Leslie Odom, Jr. (one artist) --> Leslie Odom
    / Jr. (two artists). Every combination of artist pairs will be compared to
    all others, utilizing length differences to make it more efficient, as if
    it matters with this piece of shit, and if a stripped/sanitized ordering
    matches a larger artist, the smaller/fragmented artists will be removed
    from the pool.
    """
    to_replace = construct_replacement_list(artists)
    artists = fix_artists_list(artists, to_replace)
    if tracks:
        artist_pool = _generate_artist_pool_lower_case(tracks)
        for _dnum, disc in tracks.items():
            for _tnum, track in disc.items():
                # Deduplicate the artists before passing them to fix_artists_list
                deduplicated_artists = []
                seen_normalized = set()

                for art, imp in track["artists"]:
                    normalized_art = normalize_accents(art.lower())  # Normalize and make lowercase
                    if normalized_art not in seen_normalized:
                        deduplicated_artists.append((art, imp))  # Add the original artist with the importance
                        seen_normalized.add(normalized_art)

                track["artists"] = fix_artists_list(
                    [
                        (artist_pool[normalize_accents(art.lower())], imp)
                        for art, imp in deduplicated_artists
                    ],
                    to_replace,
                )
    return artists, tracks


def construct_replacement_list(artists):
    """
    Create the list of artists-to-replace. It compares a stripped version
    of each artist to combined versions of other artists in ascending
    length order.
    """
    to_replace = []
    artist_pool = sorted(
        [
            [
                normalize_accents(
                    "".join(s for s in a if s.isalnum()).replace(" ", "")
                ).lower(),
                a,
            ]
            for a, _ in artists
        ],
        key=lambda a: len(a),
    )
    for i, pri_a_raw in enumerate(artist_pool):
        for other_a in reversed(artist_pool[0:i]):
            current_replacements = [pri_a_raw[1]]
            pri_a = copy(pri_a_raw)
            pri_a[0] = other_a[0] + pri_a[0]
            current_replacements.append(other_a[1])
            for artist_to_compare in artist_pool[i:]:
                if pri_a[0] == artist_to_compare[0]:
                    to_replace.append((current_replacements, artist_to_compare[1]))
    return to_replace


def fix_artists_list(original_artists, to_replace):
    """
    Iterate over the replacement list and remove any artists
    that need to be replaced. If the replacement is not present in the
    artists list, add it. All artist types are iterated through individually.
    """
    artists_by_type = defaultdict(list)
    for artist, importa in original_artists:
        artists_by_type[importa].append(artist)

    for artist_type, artists in artists_by_type.items():
        for replaceds, replacement in to_replace:
            found = False
            for artist in sorted(artists, key=lambda a: len(a)):
                if (
                    any(artist == r for r in replaceds)
                    and (artist, artist_type) in original_artists
                ):
                    original_artists.remove((artist, artist_type))
                else:
                    continue
                if artist == replacement:
                    found = True
            if not found and (replacement, artist_type) not in original_artists:
                original_artists.append((replacement, artist_type))

    return original_artists


def append_remixers_to_track_titles(data):
    for dnum, disc in data.items():
        for tnum, track in disc.items():
            remix_artists = [a for a, i in track["artists"] if i == "remixer"]
            if "Remix" not in track["title"]:
                if len(remix_artists) >= config.VARIOUS_ARTIST_THRESHOLD:
                    data[dnum][tnum]["title"] += " (Remixed)"
                elif remix_artists:
                    data[dnum][tnum]["title"] += f' ({" & ".join(remix_artists)} Remix)'

    return data


def assign_track_totals(data):
    for dnum, disc in data.items():
        for tnum, _track in disc.items():
            data[dnum][tnum]["tracktotal"] = len(disc)
            data[dnum][tnum]["disctotal"] = len(data)
    return data


def standardize_genres(genre_set):
    new_set = set()
    for g in genre_set:
        try:
            new_set |= fetch_genre(g)
        except GenreNotInWhitelist:
            new_set.add(g)
    return list(new_set)
