import re
from collections import defaultdict

from salmon.common import RE_FEAT, parse_copyright
from salmon.errors import ScrapeError
from salmon.sources import AppleMusicBase
from salmon.tagger.sources.base import MetadataMixin

ALIAS_GENRE = {
    "Hip-Hop/Rap": {"Hip Hop", "Rap"},
    "R&B/Soul": {"Rhythm & Blues", "Soul"},
    "Music": {},  # Aliasing Music to an empty set because we don't want a genre 'music'
}


class Scraper(AppleMusicBase, MetadataMixin):
    def parse_release_title(self, soup):
        """Parse release title from amp-api album attributes."""
        try:
            title = soup["album"].get("name", "").strip()
            if not title:
                raise ScrapeError("No album name found in Apple Music API response")
            return RE_FEAT.sub("", title)
        except (TypeError, KeyError) as e:
            raise ScrapeError("Failed to parse release title from Apple Music API") from e

    def parse_cover_url(self, soup):
        """Parse cover URL from amp-api album artwork.

        The artwork URL template uses ``{w}x{h}bb.jpg``; we substitute a high
        resolution size so the scraper returns a full-quality image.
        """
        try:
            artwork = soup["album"].get("artwork", {}) or {}
            url_template = artwork.get("url", "")
            if not url_template:
                return None
            return url_template.replace("{w}x{h}", "5000x5000")
        except (TypeError, KeyError) as e:
            raise ScrapeError("Could not parse cover URL from Apple Music API") from e

    def parse_genres(self, soup):
        """Parse genres from amp-api album genreNames list."""
        try:
            genre_names = soup["album"].get("genreNames") or []
            genres: set[str] = set()
            for genre in genre_names:
                if not genre:
                    continue
                aliased = ALIAS_GENRE.get(genre)
                if aliased is not None:
                    genres.update(aliased)
                else:
                    genres.add(genre)
            return genres
        except (TypeError, KeyError) as e:
            raise ScrapeError("Could not parse genres from Apple Music API") from e

    def parse_release_year(self, soup):
        """Parse release year from amp-api album releaseDate."""
        try:
            release_date = soup["album"].get("releaseDate", "")
            if release_date:
                year_match = re.search(r"(\d{4})", release_date)
                if year_match:
                    return int(year_match.group(1))
            raise ScrapeError("No valid release date found in Apple Music API response")
        except (TypeError, ValueError) as e:
            raise ScrapeError("Could not parse release year from Apple Music API") from e

    def parse_release_type(self, soup):
        """Parse release type from amp-api album attributes.

        amp-api exposes ``isSingle`` and ``isCompilation`` boolean flags, and
        the ``name`` field may contain EP/Single suffixes.
        """
        try:
            attrs = soup["album"]
            name = attrs.get("name", "").strip()
            track_count = attrs.get("trackCount", 0) or 0

            if re.search(r"\bE\.?P\.?\b", name, re.IGNORECASE):
                return "EP"
            if re.search(r"-? *Single$", name, re.IGNORECASE):
                return "Single"

            if attrs.get("isSingle"):
                return "Single"
            if attrs.get("isCompilation"):
                return "Compilation"

            if track_count == 1:
                return "Single"
            if track_count <= 6:
                return "EP"
            return "Album"
        except (TypeError, KeyError) as e:
            raise ScrapeError("Could not parse release type from Apple Music API") from e

    def parse_release_date(self, soup):
        """Parse full release date (YYYY-MM-DD) from amp-api album attributes."""
        try:
            release_date = soup["album"].get("releaseDate", "")
            if release_date:
                return release_date.split("T")[0]
            return None
        except (TypeError, KeyError):
            return None

    def parse_release_label(self, soup):
        """Parse record label from amp-api album attributes."""
        try:
            attrs = soup["album"]
            # amp-api returns recordLabel directly
            label = attrs.get("recordLabel", "")
            if label:
                return label
            # Fall back to copyright string parsing
            copyright_text = attrs.get("copyright", "")
            if copyright_text:
                return parse_copyright(copyright_text)
            return attrs.get("artistName", "Unknown")
        except (TypeError, KeyError) as e:
            raise ScrapeError("Could not parse record label from Apple Music API") from e

    def parse_upc(self, soup):
        """Parse UPC from amp-api album attributes."""
        try:
            return soup["album"].get("upc") or None
        except (TypeError, KeyError):
            return None

    def parse_comment(self, soup):
        """amp-api does not provide editorial notes in the catalog endpoint."""
        return None

    async def parse_tracks(self, soup):
        """Parse tracks from amp-api relationships.tracks.data list.

        Each entry in ``soup["tracks"]`` is a catalog song object with an
        ``attributes`` dict containing name, artistName, trackNumber,
        discNumber, isrc, etc.
        """
        tracks: dict[str, dict] = defaultdict(dict)

        try:
            track_list = soup.get("tracks", [])
            if not track_list:
                raise ScrapeError("No tracks found in Apple Music API response")

            for item in track_list:
                # Skip music-video or other non-song entries
                if item.get("type") not in ("songs", None):
                    continue

                attrs = item.get("attributes", {}) or {}
                track_num = attrs.get("trackNumber", 0) or 0
                disc_num = attrs.get("discNumber", 1) or 1

                if track_num <= 0:
                    continue

                raw_title = attrs.get("name", "").strip()
                title = RE_FEAT.sub("", raw_title)

                artists: list[tuple[str, str]] = []
                artist_name = attrs.get("artistName", "")
                if artist_name:
                    artists.append((artist_name, "main"))

                # Extract featured artists embedded in track title
                feat_match = RE_FEAT.search(raw_title)
                if feat_match:
                    for guest in _parse_guest_artists(feat_match.group(1)):
                        if (guest, "guest") not in artists:
                            artists.append((guest, "guest"))

                tracks[str(disc_num)][track_num] = self.generate_track(
                    trackno=track_num,
                    discno=disc_num,
                    artists=artists,
                    title=title,
                    isrc=attrs.get("isrc"),
                    explicit=attrs.get("contentRating") == "explicit",
                )

            if not tracks:
                raise ScrapeError("No valid song tracks found in Apple Music API response")

            return dict(tracks)

        except (TypeError, KeyError, ValueError) as e:
            raise ScrapeError("Could not parse tracks from Apple Music API") from e


def _parse_guest_artists(artist_string: str) -> list[str]:
    """Parse guest artists from a feature string.

    Args:
        artist_string: Raw featured artist string, e.g. "Artist A & Artist B".

    Returns:
        Deduplicated list of artist name strings.
    """
    separators = [" & ", " and ", ", ", " feat. ", " ft. ", " featuring "]
    normalized = artist_string
    for sep in separators:
        normalized = normalized.replace(sep, " | ")

    seen: set[str] = set()
    result: list[str] = []
    for artist in normalized.split(" | "):
        artist = artist.strip()
        if artist and artist not in seen:
            seen.add(artist)
            result.append(artist)
    return result
