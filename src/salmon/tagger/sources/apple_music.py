import re
from collections import defaultdict
from itertools import pairwise

from salmon.common import RE_FEAT, parse_copyright
from salmon.common.strings import split_type_marker
from salmon.errors import ScrapeError
from salmon.sources import AppleMusicBase
from salmon.tagger.sources.base import MetadataMixin, add_track

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
            if split_type_marker(name, bare=False).rls_type == "Single":
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
        discNumber, isrc, etc. When the relationship holds the songs' artists
        one by one, in Apple's order, they become separate main credits, except
        for any the title credits as a guest. The ``artistName`` display string
        is used only if the relationship is missing or incomplete: splitting it
        would break names like "Simon & Garfunkel" that hold a separator.
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
                artist_rels = (item.get("relationships") or {}).get("artists") or {}
                rel_names: list[str] = []
                for rel in artist_rels.get("data") or []:
                    name = (rel.get("attributes") or {}).get("name", "").strip()
                    if rel.get("type") == "artists" and name and name not in rel_names:
                        rel_names.append(name)

                # A title's feat. clause lists guests in free text, even when Apple also lists them in the
                # relationship. Match whole relationship names first so a guest like "Simon & Garfunkel"
                # stays a single guest and is excluded from mains.
                feat_match = RE_FEAT.search(raw_title)
                guests = _extract_title_guests(feat_match.group(1), rel_names) if feat_match else []
                guest_keys = {guest.casefold() for guest in guests}

                artist_name = (attrs.get("artistName") or "").strip()
                explicit_main_artists = False
                # Apple leaves artists without an artist page out of the relationship. Use it only when its
                # names account for the whole display string; otherwise the string is all we have.
                if artist_name and _covers_artist_name(rel_names, artist_name):
                    for name in rel_names:
                        if name.casefold() not in guest_keys and (name, "main") not in artists:
                            artists.append((name, "main"))
                    explicit_main_artists = bool(artists)

                # Older/cached responses without the artists, or an incomplete relationship: keep the
                # display string whole, so a name with no Apple artist page isn't lost or mis-split.
                if not artists and artist_name:
                    artists.append((artist_name, "main"))

                for guest in guests:
                    if (guest, "guest") not in artists:
                        artists.append((guest, "guest"))

                new_track = self.generate_track(
                    trackno=track_num,
                    discno=disc_num,
                    artists=artists,
                    title=title,
                    isrc=attrs.get("isrc"),
                    explicit=attrs.get("contentRating") == "explicit",
                )
                if explicit_main_artists:
                    # These main credits are stated per artist by the store, not inferred from a joined string:
                    # the split-repair must not join them back by name. Title guests remain repairable. The flag
                    # is per track, so other stores' main credits merged into it skip the repair too; see
                    # TrackMetadata.explicit_main_artists until Apple's credits become stated claims (ADR 0007).
                    new_track["explicit_main_artists"] = True
                add_track(tracks, str(disc_num), track_num, new_track)

            if not tracks:
                raise ScrapeError("No valid song tracks found in Apple Music API response")

            return dict(tracks)

        except (TypeError, KeyError, ValueError) as e:
            raise ScrapeError("Could not parse tracks from Apple Music API") from e


_RE_ARTIST_GAP = re.compile(
    r"^(?:\s*(?:,\s*)?[&,;/\\]\s*|(?:,\s*|\s+)(?:and|feat|ft|featuring|with)\.?\s+)$",
    re.IGNORECASE,
)


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _is_whole_token(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` is not embedded inside a larger word."""
    left_ok = start == 0 or not _is_word_char(text[start]) or not _is_word_char(text[start - 1])
    right_ok = end == len(text) or not _is_word_char(text[end - 1]) or not _is_word_char(text[end])
    return left_ok and right_ok


def _covers_artist_name(names: list[str], artist_name: str) -> bool:
    """Whether named artists account for the whole display string, with only separators between them.

    Apple's relationship leaves out artists without an artist page. If it only accounts for part of
    ``artistName``, the missing name must come from the display string, kept whole.
    """
    if not names:
        return False
    matched: list[tuple[int, int]] = []
    for name in sorted(names, key=len, reverse=True):
        for match in re.finditer(re.escape(name), artist_name, flags=re.IGNORECASE):
            start, end = match.start(), match.end()
            if _is_whole_token(artist_name, start, end) and not any(s < end and start < e for s, e in matched):
                matched.append((start, end))
                break
    if not matched:
        return False
    matched.sort()
    if artist_name[: matched[0][0]].strip() or artist_name[matched[-1][1] :].strip():
        return False
    return all(_RE_ARTIST_GAP.match(artist_name[end:next_start]) for (_, end), (next_start, _) in pairwise(matched))


_RE_GUEST_SEP_END = re.compile(r"(?:[&,;/\\]|\s(?:and|feat|ft|featuring|with)\.?)\s*$", re.IGNORECASE)
_RE_GUEST_SEP_START = re.compile(r"^\s*(?:[&,;/\\]|(?:and|feat|ft|featuring|with)\.?(?:\s|$))", re.IGNORECASE)


def _is_guest_unit(feat_text: str, start: int, end: int) -> bool:
    """Whether ``feat_text[start:end]`` stands as a complete guest entry rather than words inside another name."""
    before = feat_text[:start]
    after = feat_text[end:]
    left_ok = not before.strip() or bool(_RE_GUEST_SEP_END.search(before))
    right_ok = not after.strip() or bool(_RE_GUEST_SEP_START.match(after))
    return left_ok and right_ok


def _extract_title_guests(feat_text: str, rel_names: list[str]) -> list[str]:
    """Parse guest artists from a title's feat. clause, keeping whole relationship names intact."""
    matched: list[tuple[int, int, str]] = []
    for name in sorted(rel_names, key=len, reverse=True):
        for match in re.finditer(re.escape(name), feat_text, flags=re.IGNORECASE):
            start, end = match.start(), match.end()
            if _is_guest_unit(feat_text, start, end) and not any(s < end and start < e for s, e, _ in matched):
                matched.append((start, end, name))
                break
    if not matched:
        return _parse_guest_artists(feat_text)

    matched.sort()
    guests: list[str] = []
    seen: set[str] = set()
    pos = 0
    for start, end, name in matched:
        for guest in _parse_guest_artists(feat_text[pos:start]):
            guest = guest.strip(" &,;/\\")
            if guest and guest.casefold() not in seen:
                seen.add(guest.casefold())
                guests.append(guest)
        if name.casefold() not in seen:
            seen.add(name.casefold())
            guests.append(name)
        pos = end
    for guest in _parse_guest_artists(feat_text[pos:]):
        guest = guest.strip(" &,;/\\")
        if guest and guest.casefold() not in seen:
            seen.add(guest.casefold())
            guests.append(guest)
    return guests


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
