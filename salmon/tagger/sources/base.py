import re
from abc import ABC, abstractmethod
from collections import defaultdict
from copy import copy
from itertools import chain
from typing import Any

from salmon import cfg
from salmon.common import fetch_genre, less_uppers, normalize_accents
from salmon.errors import GenreNotInWhitelist


class MetadataMixin(ABC):
    # These methods are expected to be provided by BaseScraper when used as a mixin
    format_url: Any  # Provided by BaseScraper subclass
    create_soup: Any

    async def scrape_release_from_id(self, rls_id: str) -> dict[str, Any]:
        """Run a scrape from the release ID."""
        return await self.scrape_release(self.format_url(rls_id=rls_id), rls_id=rls_id)

    async def scrape_release(self, url, rls_id=None):
        """
        Scrape the metadata of a release and return a dictionary of scraped data.
        Data may vary depending on the source; unavailable keys will be left
        as None.
        """
        soup = await self.create_soup(url)

        tracks_result = await self.parse_tracks(soup)

        data = {
            "title": self.parse_release_title(soup),
            "cover": self.parse_cover_url(soup),
            "genres": standardize_genres(
                [
                    g
                    for g in self.parse_genres(soup)
                    if g.lower() not in [c.lower() for c in cfg.upload.search.blacklisted_genres]
                ]
            ),
            "year": self.parse_release_year(soup),
            "group_year": self.parse_release_group_year(soup),
            "date": self.parse_release_date(soup),
            "edition_title": self.parse_edition_title(soup),
            "label": self.parse_release_label(soup),
            "catno": self.parse_release_catno(soup),
            "rls_type": self.parse_release_type(soup),
            "tracks": tracks_result,
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
        trackno: int | str,
        discno: int | str,
        artists: list[tuple[str, str]],
        title: str,
        replay_gain: float | None = None,
        peak: float | None = None,
        format_: str | None = None,
        explicit: bool | None = None,
        isrc: str | None = None,
        stream_id: str | int | None = None,
        streamable: bool | None = None,
        md5_origin: str | None = None,
        media_version: str | None = None,
        lossless: bool | None = None,
        mp3_320: bool | None = None,
    ) -> dict[str, Any]:
        """Return a generated track dictionary containing the required values.

        Args:
            trackno: Track number.
            discno: Disc number.
            artists: List of (artist_name, role) tuples.
            title: Track title.
            replay_gain: Replay gain value.
            peak: Peak value.
            format_: Audio format/quality.
            explicit: Whether the track is explicit.
            isrc: ISRC code.
            stream_id: Stream ID for streaming services.
            streamable: Whether the track is streamable.
            md5_origin: MD5 origin hash (Deezer).
            media_version: Media version (Deezer).
            lossless: Whether lossless is available (Deezer).
            mp3_320: Whether MP3 320 is available (Deezer).

        Returns:
            A dictionary containing track metadata.
        """
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
            "md5_origin": md5_origin,
            "media_version": media_version,
            "lossless": lossless,
            "mp3_320": mp3_320,
        }

    def determine_rls_type(self, data):
        def strip_base_title(title):
            return re.sub(r"\s*\(.*?\)", "", title).strip().lower()

        tracks = [track for disc in data["tracks"].values() for track in disc.values()]

        num_tracks = len(tracks)
        base_titles = {strip_base_title(track["title"]) for track in tracks}
        main_artists = {artist[0] for artist in data.get("artists", []) if artist[1] == "main"}

        title = data["title"]
        rls_type = data.get("rls_type").lower() if data.get("rls_type") else ""

        # --- Title-based overrides ---
        if re.search(r"\bE\.?P\.?\b", title, re.IGNORECASE):
            return re.sub(r"\bE\.?P\.?\b", "", title, flags=re.IGNORECASE).strip(), "EP"
        if re.search(r"-? *Single$", title, re.IGNORECASE):
            return re.sub(r"-? *Single$", "", title, flags=re.IGNORECASE).strip(), "Single"
        elif re.search(r"original.*soundtrack", data["title"], flags=re.IGNORECASE):
            return data["title"], "Soundtrack"

        # --- Explicit rls_type ---
        if rls_type == "soundtrack":
            return title, "Soundtrack"
        if rls_type == "compilation" and len(main_artists) <= 2:
            return title, "Anthology"

        # --- Track-based inference ---
        if num_tracks <= 3 or len(base_titles) <= 2:
            return title, "Single"
        if num_tracks <= 7 and (not data["rls_type"] or data["rls_type"] == "EP"):
            return title, "EP"

        # At that point, it should be a kind of album, but which one
        remix_count = sum(1 for t in tracks if re.search(r"(mix|remix)", t["title"], re.IGNORECASE))
        if remix_count / max(1, num_tracks) >= 0.5:
            return title, "Remix"
        # If rls_type was actually passed (but not album, we have other possibilities), let's try to return that
        if rls_type and rls_type != "album":
            return title, data.get("rls_type")

        # An album with 6+ main artists is surely a compilation
        if len(main_artists) >= 6:
            return title, "Compilation"

        # If the title contains "live", it's a live album?
        if "live" in title.lower():
            return title, "Live album"

        # No choice left I guess
        return title, "Album"

    @abstractmethod
    def parse_release_title(self, soup) -> str | None:
        pass

    @abstractmethod
    def parse_release_year(self, soup) -> int | None:
        pass

    @abstractmethod
    def parse_release_label(self, soup) -> str | None:
        pass

    @abstractmethod
    async def parse_tracks(self, soup) -> dict:
        pass

    # The below parsers aren't present in every scraper.

    def parse_release_group_year(self, soup) -> int | None:
        return self.parse_release_year(soup)

    def parse_cover_url(self, soup) -> str | None:
        return None

    def parse_release_date(self, soup) -> str | None:
        return None

    def parse_edition_title(self, soup) -> str | None:
        return None

    def parse_release_catno(self, soup) -> str | None:
        return None

    def parse_release_type(self, soup) -> str | None:
        return None

    def parse_genres(self, soup) -> set | list:
        return set()

    def parse_upc(self, soup) -> str | None:
        return None

    def parse_comment(self, soup) -> str | None:
        return None

    def process_label(self, data):
        """
        Process the label field in the metadata.
        """
        label = data.get("label", "")
        return determine_label_type(label, data["artists"])

    @staticmethod
    def parse_title(title, version):
        """
        Return a filtered title; all those parenthetical phrases belong
        in album info. We also filter out featured artists, since those are
        parsed with the artists.
        """
        if cfg.upload.formatting.strip_useless_versions:
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


def determine_label_type(label, artists):
    """
    Determine the type of label based on the label and artists.
    Return "Self-Released" if self-released, otherwise return the original label.
    """

    def _compare(label, artist):
        label, artist = label.lower(), artist.lower()
        return label == artist or label.startswith(artist)

    if isinstance(label, str):
        # Check for "Not On Label" or "Self-Released" in the label
        if re.search(r"(not on label|no label|self[- ]?released)", label, re.IGNORECASE):
            return "Self-Released"

        # Compare label to artist name
        if any(_compare(label, str(a)) and i == "main" for a, i in artists):
            return "Self-Released"
    return label


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
                    [(artist_pool[normalize_accents(art.lower())], imp) for art, imp in deduplicated_artists],
                    to_replace,
                )
    return artists, tracks


def construct_replacement_list(artists: list[tuple[str, str]]) -> list[tuple[list[str], str]]:
    """
    Create the list of artists-to-replace. It compares a stripped version
    of each artist to combined versions of other artists in ascending
    length order.

    Args:
        artists: List of (artist_name, importance) tuples.

    Returns:
        List of (replacements, replacement_artist) tuples.
    """
    to_replace: list[tuple[list[str], str]] = []
    # Build artist pool with normalized names
    unsorted_pool: list[list[str]] = []
    for artist_name, _ in artists:
        stripped = "".join(s for s in str(artist_name) if s.isalnum()).replace(" ", "")
        normalized_result = normalize_accents(stripped)
        # normalize_accents returns str when given single argument
        normalized = str(normalized_result).lower() if normalized_result else ""
        unsorted_pool.append([normalized, str(artist_name)])
    artist_pool: list[list[str]] = sorted(unsorted_pool, key=lambda x: len(x[0]))
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
                if any(artist == r for r in replaceds) and (artist, artist_type) in original_artists:
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
            if not any(x in track["title"] for x in ("Remix", "Mix")):
                if len(remix_artists) >= cfg.upload.formatting.various_artist_threshold:
                    data[dnum][tnum]["title"] += " (Remixed)"
                elif remix_artists:
                    data[dnum][tnum]["title"] += f" ({' & '.join(remix_artists)} Remix)"

    return data


def assign_track_totals(data):
    for dnum, disc in data.items():
        for tnum, _track in disc.items():
            data[dnum][tnum]["tracktotal"] = len(disc)
            data[dnum][tnum]["disctotal"] = len(data)
    return data


def _is_separate_word_in_combination(generic, combined):
    # Normalize to lowercase
    generic = generic.lower()
    combined_parts = re.split(r"\s*/\s*|\s*&\s*|\s+and\s+", combined.lower())
    return generic in combined_parts


def standardize_genres(genre_set):
    new_set = set()
    for g in genre_set:
        try:
            new_set |= fetch_genre(g)
        except GenreNotInWhitelist:
            new_set.add(g)

    # Filter out generic genres if more specific combos exist
    filtered = set(new_set)
    for genre in new_set:
        for other in new_set:
            if genre != other and _is_separate_word_in_combination(genre, other):
                filtered.discard(genre)
                break

    return list(filtered)
