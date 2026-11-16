import re
from collections import defaultdict
from datetime import date
from html import unescape
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from salmon import cfg
from salmon.common import RE_FEAT, parse_copyright, re_split
from salmon.common.credits import Credit, Role, RoleWords
from salmon.common.strings import comparable, guests_from_title
from salmon.errors import ScrapeError
from salmon.sources import QobuzBase
from salmon.tagger.sources.base import MetadataMixin, ends_with_ep, standardize_genres, strip_ep_suffix
from salmon.tagger.tags import is_classical_genre

# ------------------------------------------------------------------------------
# Constants and Regular Expressions
# ------------------------------------------------------------------------------

# Qobuz's words for the roles salmon maps, in a track's performers line ("Name, Role, Role - Name, Role"). Any
# other word is a role salmon doesn't map. Spellings fold: RoleWords matches "Main Artist" as "MainArtist".
QOBUZ_ROLE_WORDS: dict[str, Role | None] = {
    "MainArtist": "main",
    "FeaturedArtist": "guest",
    "Remixer": "remixer",
    "Conductor": "conductor",
    "Composer": "composer",
    "ComposerLyricist": "composer",
    "Producer": "producer",
    "Arranger": "arranger",
}
ROLE_WORDS = RoleWords(QOBUZ_ROLE_WORDS)
# The roles salmon reads from Qobuz, and composers on a classical release (#869). Producer and arranger wait for a case.
READ_ROLES: frozenset[Role] = frozenset({"main", "guest", "remixer", "conductor"})
# The parts after a comma that are still a name's, by comparable: "Sammy Davis, Jr.", "Johann Strauss, II".
NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})

# Pre-compiled regular expressions for better performance
RE_YEAR = re.compile(r"(\d{4})")
RE_EDITION = re.compile(r"\((.*?)\)(?:\s*-\s*(?:Single|EP))?$")
RE_SINGLE = re.compile(r"-? *Single$", re.IGNORECASE)
RE_SOUNDTRACK = re.compile(r"original.*soundtrack", re.IGNORECASE)

# Release type mappings
RECORD_TYPES = {
    "album": "Album",
    "ep": "EP",
    "epmini": "EP",
    "single": "Single",
}

# Genre splitting for combined genres
SPLIT_GENRES = {
    "Pop Rock": {"Pop", "Rock"},
    "Pop/Rock": {"Pop", "Rock"},
    "Pop/Rock→Pop": {"Pop", "Rock"},
    "Pop/Rock→Rock": {"Pop", "Rock"},
    "Rock & Pop": {"Rock", "Pop"},
    "Pop/Rock→Rock→Alternatif et Indé": {"Rock", "Pop", "Indie", "Alternative"},
    "Alternatif et Indé": {"Indie", "Alternative"},
    "Rock/Pop": {"Rock", "Pop"},
    "Hip-Hop/Rap": {"Hip Hop", "Rap"},
    "Hip Hop/Rap": {"Hip Hop", "Rap"},
    "R&B/Soul": {"R&B", "Soul"},
    "Soul/R&B": {"Soul", "R&B"},
    "Indie Pop": {"Indie", "Pop"},
    "Indie Rock": {"Indie", "Rock"},
    "Electronic/Dance": {"Electronic", "Dance"},
    "Dance/Electronic": {"Dance", "Electronic"},
    "House/Techno": {"House", "Techno"},
    "Techno/House": {"Techno", "House"},
    "Folk/Country": {"Folk", "Country"},
    "Country/Folk": {"Country", "Folk"},
    "Jazz Funk": {"Jazz", "Funk"},
    "Funk/Jazz": {"Funk", "Jazz"},
    "Classical/Contemporary": {"Classical", "Contemporary"},
    "Blues Rock": {"Blues", "Rock"},
    "Rock Blues": {"Rock", "Blues"},
    "Ambient/Experimental": {"Ambient", "Experimental"},
    "Trip Hop": {"Trip Hop"},  # Keep this one as is
    "Music": {},  # Skip generic 'Music' genre
}

# Common edition keywords to look for
EDITION_KEYWORDS = {
    "Live",
    "Remaster",
    "Deluxe",
    "Edition",
    "Version",
    "Anniversary",
    "Expanded",
    "Special",
    "Collector",
    "Extended",
    "Director",
    "Cut",
    "Bonus",
    "Acoustic",
    "Demo",
    "Mix",
    "Original",
    "Alternate",
    "Vinyl",
}

# ------------------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------------------


def safe_get(d, keys, default=None):
    """
    Safely access nested dictionary values without raising KeyError.
    """
    if not isinstance(d, dict):
        return default

    result = d
    for key in keys:
        if not isinstance(result, dict) or key not in result:
            return default
        result = result.get(key)

    # Return default for None/empty values
    return result if result else default


_RELEASED_ON = re.compile(r"Released on\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", re.IGNORECASE)
_DISC_HEADING = re.compile(r"^\s*Disc\s+(\d+)\s*$", re.IGNORECASE)
_TRACK_COUNT = re.compile(r"(\d+)\s+track", re.IGNORECASE)
_COVER_SIZE = re.compile(r"_\d+\.jpg$", re.IGNORECASE)
# Every locale serves every album; gb-en pins the labels to English and the date to day-first.
PUBLIC_PAGE = "https://www.qobuz.com/gb-en/album/-/{album_id}"


def _text(element) -> str:
    return element.get_text(" ", strip=True) if element else ""


def _meta_item(soup: BeautifulSoup, prefix: str, selector: str = ".album-meta__item, .album-about__item"):
    """The first list item under `selector` whose text starts with `prefix`, else None."""
    for item in soup.select(selector):
        if _text(item).lower().startswith(prefix.lower()):
            return item
    return None


def _released_on(text: str) -> str | None:
    match = _RELEASED_ON.search(text)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    # date() raises ValueError on an out-of-range day or month; let the caller turn that into a ScrapeError.
    return date(year, month, day).isoformat()


def _duration_seconds(text: str) -> int | None:
    parts = [part for part in text.strip().split(":") if part.isdecimal()]
    if not parts:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _page_tracks(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Tracks in page order; a "Disc N" heading between them moves the following ones to that disc."""
    tracks: list[dict[str, Any]] = []
    disc = 1
    for node in soup.descendants:
        if isinstance(node, NavigableString):
            heading = _DISC_HEADING.match(str(node))
            if heading:
                disc = int(heading.group(1))
        elif isinstance(node, Tag) and "track" in (node.get("class") or []):
            infos = [_text(info) for info in node.select(".track__info")]
            number = _text(node.select_one(".track__item--number span"))
            items = node.select_one(".track__items")
            title = _text(node.select_one(".track__item--name span")) or (items.get("title", "") if items else "")
            tracks.append(
                {
                    "media_number": disc,
                    "track_number": int(number) if number.isdecimal() else len(tracks) + 1,
                    "title": unescape(str(title)),
                    "duration": _duration_seconds(_text(node.select_one(".track__item--duration"))),
                    "performers": infos[0] if infos else "",
                    "copyright": infos[1] if len(infos) > 1 else None,
                }
            )
    return tracks


def page_to_api_shape(soup: BeautifulSoup) -> dict[str, Any]:
    """Read Qobuz's public album page into the dict shape the API returns, as far as the page carries it."""
    released = _meta_item(soup, "Released on")
    label_link = released.find("a") if released else None
    main_artists = _meta_item(soup, "Main artists:")
    # The "About" section lists every genre; the header shows only the first.
    genre_item = _meta_item(soup, "Genre:", ".album-about__item") or _meta_item(soup, "Genre:")
    about_count = next((item for item in soup.select(".album-about__item") if _TRACK_COUNT.search(_text(item))), None)
    tracks = _page_tracks(soup)
    count_match = _TRACK_COUNT.search(_text(about_count)) if about_count else None
    cover = soup.find("meta", property="og:image")
    cover_url = str(cover.get("content", "")) if isinstance(cover, Tag) else ""
    artist_links = main_artists.select("a") if main_artists else []
    # The header joins every main artist into one name ("A & B"); each track's own credits tell them apart.
    artist_name = _text(artist_links[0]) if artist_links else _text(soup.select_one(".album-meta__title .artist-name"))
    return {
        "title": _text(soup.select_one(".album-meta__title .album-title")),
        "artist": {"name": artist_name},
        "artists": [{"name": _text(link), "roles": ["main-artist"]} for link in artist_links],
        "label": {"name": _text(label_link)} if label_link else {},
        "release_date_original": _released_on(_text(released)) if released else None,
        "copyright": next((track["copyright"] for track in tracks if track.get("copyright")), None),
        "genres_list": list(dict.fromkeys(_text(link) for link in (genre_item.select("a") if genre_item else []))),
        "tracks_count": int(count_match.group(1)) if count_match else len(tracks),
        "tracks": {"items": tracks},
        "image": {"large": _COVER_SIZE.sub("_max.jpg", cover_url)} if cover_url else {},
        "release_type": "",
        "version": None,
        "upc": None,
    }


# ------------------------------------------------------------------------------
# Qobuz Metadata Scraper Class
# ------------------------------------------------------------------------------


class Scraper(QobuzBase, MetadataMixin):
    """
    Qobuz metadata scraper that implements MetadataMixin abstract methods
    and uses QobuzBase for authentication and API access.
    """

    states_credits = True

    # --------------------------------------------------------------------------
    # Core API and Connection Methods
    # --------------------------------------------------------------------------

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
        """Override fetch_data to properly get the album data from the API.

        This method uses the QobuzBase get_json method.

        Args:
            url: The Qobuz album URL.
            params: Optional query parameters (unused).
            headers: Optional HTTP headers (unused).
            follow_redirects: Whether to follow redirects (unused).

        Returns:
            Album data dict from Qobuz API.

        Raises:
            ScrapeError: If fetching fails.
        """
        try:
            match = self.regex.match(url)
            if not match:
                raise ScrapeError(f"Failed to extract release ID from URL: {url}")
            rls_id = match[1]
        except (TypeError, IndexError) as err:
            raise ScrapeError(f"Failed to extract release ID from URL: {url}") from err

        if not self.configured():
            # The API needs an account; the public album page does not.
            return await self._fetch_public_page(rls_id)

        try:
            response = await self.get_json(self.release_format.format(rls_id=rls_id), headers=self.headers)
        except Exception as err:
            raise ScrapeError(f"Failed to fetch data from Qobuz API: {str(err)}") from err

        if "error" in response:
            raise ScrapeError(f"Qobuz API error: {response['error']}")

        # Verify basic required fields exist
        if not response.get("title"):
            raise ScrapeError("Missing required field 'title' in Qobuz API response")

        return response

    async def _fetch_public_page(self, album_id: str) -> dict[str, Any]:
        """Album facts from the public web page, shaped like the API answer so the same parsers read them."""
        url = PUBLIC_PAGE.format(album_id=album_id)
        soup = await self.fetch_page(url)
        try:
            data = page_to_api_shape(soup)
        except ValueError as err:
            raise ScrapeError(f"Qobuz page carries an invalid release date: {url}") from err
        if not data.get("title"):
            raise ScrapeError(f"Qobuz page holds no album data (is it an album URL?): {url}")
        return data

    @classmethod
    def format_url(cls, rls_id: Any = None, rls_name: str | None = None, url: str | None = None) -> str:
        """Format a URL for the release based on ID or original URL.

        Args:
            rls_id: The release ID.
            rls_name: Optional release name (unused).
            url: Optional pre-formatted URL to return directly.

        Returns:
            The formatted URL string.
        """
        if url:
            return url
        return f"https://www.qobuz.com/album/-/{rls_id}"

    # --------------------------------------------------------------------------
    # Required Metadata Methods (from base.MetadataMixin)
    # --------------------------------------------------------------------------

    def parse_release_title(self, soup):
        """Parse the release title from the API response."""
        return RE_FEAT.sub("", soup["title"])

    def parse_release_group_year(self, soup) -> int | None:
        date = safe_get(soup, ["release_date_original"])
        if not date or not isinstance(date, str):
            return None
        match = RE_YEAR.search(date)
        return int(match.group(1)) if match else None

    def parse_release_year(self, soup) -> int | None:
        edition_title = self.parse_edition_title(soup)
        if edition_title:
            if any(keyword in edition_title for keyword in EDITION_KEYWORDS):
                copyright_text = safe_get(soup, ["copyright"])
                if not copyright_text or not isinstance(copyright_text, str):
                    return None
                match = RE_YEAR.search(copyright_text)
                return int(match.group(1)) if match else None
            else:
                return None
        else:
            return self.parse_release_group_year(soup)

    def parse_release_label(self, soup) -> str | None:
        """
        Parse label name, also from the copyright information when the API gives none. Whether it is
        Self-Released is decided by ``process_label``, as for the other sources.
        """
        # Try to get label directly from API
        label = safe_get(soup, ["label", "name"])

        # If not available, try to extract from copyright
        if not label:
            copyright_text = soup.get("copyright", "")
            if copyright_text:
                extracted_label = parse_copyright(copyright_text)
                if extracted_label:
                    label = extracted_label

        # If still no label, return None
        if not label:
            return None

        return str(label)

    async def parse_tracks(self, soup):
        """Parse track information from the API response."""
        tracks = defaultdict(dict)
        album_mains = _album_mains(soup)
        featured = _album_featured(soup, album_mains)
        album_names = {_key(name) for name in [*album_mains, *_album_listed(soup)]}
        classical = _is_classical(soup)

        track_items = safe_get(soup, ["tracks", "items"], [])
        if not isinstance(track_items, list):
            return {}

        for track in track_items:
            disc_number = str(track.get("media_number", 1))
            track_number = str(track.get("track_number", 1))
            artists, guessed = _track_credits(track, album_mains, featured, album_names, classical)

            # Parse track title with version
            title = track.get("title", "")
            if version := track.get("version"):
                title = f"{title} ({version})"

            # Create track entry
            tracks[disc_number][track_number] = self.generate_track(
                trackno=track_number,
                discno=disc_number,
                artists=list(artists),
                title=title,
                isrc=track.get("isrc"),
                explicit=track.get("parental_warning", False),
            )
            tracks[disc_number][track_number]["assumed_artists"] = guessed

        return dict(tracks)

    # --------------------------------------------------------------------------
    # Optional Metadata Methods
    # --------------------------------------------------------------------------

    def parse_cover_url(self, soup) -> str | None:
        """
        Parse the cover URL from the API response.
        Qobuz already compresses their images, so using the large image is best.
        """
        result = safe_get(soup, ["image", "large"])
        return str(result) if result else None

    def parse_edition_title(self, soup):
        """
        Extract edition information from the API response.
        Prioritizes Qobuz's explicit version field, then checks title for edition keywords.
        """
        # First check if Qobuz provides a specific version field
        if version := soup.get("version"):
            return version

        # Check for edition information in the title
        title = soup.get("title", "")
        if not title:
            return None

        # Extract text in parentheses at the end of the title
        match = RE_EDITION.search(title)
        if not match:
            return None

        edition_text = match.group(1).strip()

        # Only return if it contains a known edition keyword
        edition_lower = edition_text.lower()
        if any(keyword.lower() in edition_lower for keyword in EDITION_KEYWORDS):
            return edition_text

        return None

    def parse_release_date(self, soup):
        """Parse the release date from the API response."""
        return soup.get("release_date_original")

    def parse_release_type(self, soup):
        """
        Parse the release type from the API response.
        Returns a standardized release type based on Qobuz data.
        """
        # Try to get directly from Qobuz's mapping first
        qobuz_type = soup.get("release_type", "").lower()
        if qobuz_type in RECORD_TYPES:
            return RECORD_TYPES[qobuz_type]

        # Check the title for explicit indicators
        title = soup.get("title", "")
        if ends_with_ep(title):
            soup["title"] = strip_ep_suffix(title)
            return "EP"
        elif RE_SINGLE.search(title):
            # Remove the suffix from the title
            soup["title"] = RE_SINGLE.sub("", title)
            return "Single"
        elif RE_SOUNDTRACK.search(title):
            return "Soundtrack"

        # Simple fallback based on track count
        track_count = soup.get("tracks_count", 0)
        if track_count < 3:
            return "Single"
        if track_count < 5:
            return "EP"

        # Default to Album
        return "Album"

    def parse_genres(self, soup):
        """Parse the genres from the API response."""
        if cfg.metadata.qobuz.no_genres_from_qobuz:
            return set()
        genres = {g for gs in soup.get("genres_list") for g in SPLIT_GENRES.get(gs, [gs])}
        return genres

    def parse_upc(self, soup):
        """Parse the UPC from the API response."""
        return soup.get("upc")

    def parse_comment(self, soup):
        """Parse any comments from the API response."""
        return None
        # return soup.get("description") # This will return release notes in html format (better to keep disabled)

    # Override base.py's determine_rls_type to respect Qobuz's explicit release type.
    def determine_rls_type(self, data):
        """
        Override the base class's determine_rls_type method to prioritize
        the release type that was explicitly provided by Qobuz.
        """
        # If Qobuz provided an explicit release type, respect it and keep it
        # Might have to check which other rls_types from Qobuz are trustworthy
        if data["rls_type"] == "EP":
            # The title may still carry the marker, as with any other store: the same rule drops it.
            return strip_ep_suffix(data["title"]), "EP"
        if data["rls_type"] in ["Single", "Soundtrack"]:
            return data["title"], data["rls_type"]

        # Otherwise, fall back to the base class's heuristics
        return super().determine_rls_type(data)


# ------------------------------------------------------------------------------
# Credits (#869): read as Qobuz states them, for the merge to decide (ADR 0007)
# ------------------------------------------------------------------------------


def _key(name: str) -> str:
    """One key for the spellings of a name within Qobuz's own credits ("Sigur Ros " is "Sigur Rós")."""
    return comparable(name) or name.strip()


def _album_mains(album: dict[str, Any]) -> list[str]:
    """The album's main artists: its `artist`, then each other `artists[]` main-artist, in the spelling `artists[]`
    gives them (Getz/Gilberto's "João Gilberto", which the performers line writes "Joao")."""
    listed = [
        name
        for entry in _album_artists(album)
        if "main-artist" in (entry.get("roles") or []) and (name := str(entry.get("name") or "").strip())
    ]
    first = str(safe_get(album, ["artist", "name"]) or "").strip()
    mains: dict[str, str] = {}
    for name in [first, *listed] if first else listed:
        mains.setdefault(_key(name), name)
    spelled = {_key(name): name for name in reversed(listed)}
    return [spelled.get(key, name) for key, name in mains.items()]


def _album_featured(album: dict[str, Any], album_mains: list[str]) -> list[str]:
    """The album's featured artists (any `artists[]` role holding "feat"), guests on every track."""
    mains = {_key(name) for name in album_mains}
    return [
        name
        for entry in _album_artists(album)
        if (name := str(entry.get("name") or "").strip())
        and _key(name) not in mains
        and any("feat" in str(role).lower() for role in entry.get("roles") or [])
    ]


def _album_listed(album: dict[str, Any]) -> list[str]:
    """Every name `artists[]` lists, whatever its role (an orchestra, a conductor)."""
    return [name for entry in _album_artists(album) if (name := str(entry.get("name") or "").strip())]


def _album_artists(album: dict[str, Any]) -> list[dict[str, Any]]:
    artists = album.get("artists")
    return [entry for entry in artists if isinstance(entry, dict)] if isinstance(artists, list) else []


def _is_classical(album: dict[str, Any]) -> bool:
    """Whether Qobuz files the release under a classical genre: its own genres_list, read whatever
    no_genres_from_qobuz says, through salmon's genre table ("Classique→Opéra" is Classical and Opera)."""
    genres = [str(genre) for genre in album.get("genres_list") or [] if genre]
    return any(is_classical_genre(genre) for genre in standardize_genres(genres))


def _performers(line: str, known: set[str]) -> list[tuple[str, list[Role | None]]]:
    """Each name in a performers line, "A, Role, Role - B, Role", with the roles of its words; an entry with no mapped
    role credits no one.

    A name is the entry's first part, as master read it: Qobuz's other words are an open list ("Harpsichord",
    "Guitar (acoustic)"). It runs on over commas only to a name Qobuz gives elsewhere as a name (`known`: the album's
    artists and the track's performer), as "Earth, Wind & Fire", or over suffixes, as "Sammy Davis, Jr.".
    """
    entries = []
    for entry in line.split(" - "):
        parts = entry.split(",")
        first = next((i for i, part in enumerate(parts) if i and ROLE_WORDS.role_of(part)), None)
        if first is None:
            continue
        size = next(
            (
                k
                for k in range(first, 1, -1)
                if _key(",".join(parts[:k])) in known or all(comparable(part) in NAME_SUFFIXES for part in parts[1:k])
            ),
            1,
        )
        if name := ",".join(parts[:size]).strip(" ,"):
            entries.append((name, [ROLE_WORDS.role_of(part) for part in parts[size:]]))
    return entries


def _track_credits(
    track: dict[str, Any], album_mains: list[str], featured: list[str], album_names: set[str], classical: bool
) -> tuple[list[Credit], list[Credit]]:
    """A track's credits as Qobuz states them, and those of them that are salmon's guesses (ADR 0007).

    Stated: the album's main artists the track names, as its performer or a MainArtist in its performers line, or
    every one of them when it names no one; the line's guests, remixers and conductors; the album's featured artists;
    and on a classical release the track's composer. Guessed: a MainArtist the album doesn't list as main, a guest
    read from "feat." in a performer's name or in the title, both guests by salmon's reading, and the album's main
    artists on a track that names only others. Nothing is decided here: one name may hold two roles, for the merge to
    weigh.
    """
    album = {_key(name): name for name in album_mains}
    named: set[str] = set()
    others = False  # whether the track names a main artist the album doesn't list as one
    read: list[tuple[str, Role, bool]] = []  # (name, role, assumed), in the order Qobuz gives them

    def as_main(name: str) -> None:
        nonlocal others
        lead = RE_FEAT.sub("", name).strip()
        if _key(lead) in album:
            named.add(_key(lead))
        elif not any(_key(part) in album for part in re_split(lead)):
            # A joint name holding an album main ("Nas & Damian Marley") is the album's credit: it names no one else.
            read.append((lead, "guest", True))
            others = True
        read.extend((guest, "guest", True) for guest in guests_from_title(name))

    performer = str(safe_get(track, ["performer", "name"]) or "").strip()
    known = album_names | ({_key(performer)} if performer else set())
    if performer:
        as_main(performer)
    line_composers: list[str] = []
    for name, roles in _performers(track.get("performers") or "", known):
        for role in dict.fromkeys(roles):
            if role == "main":
                as_main(name)
            elif role in READ_ROLES:
                read.append((name, role, False))
            elif role == "composer":
                line_composers.append(name)

    mains = [name for key, name in album.items() if key in named]
    # A track naming none of the album's mains gets them all: as Qobuz's word if it names no one, else as a guess.
    claims: list[tuple[str, Role, bool]] = [(name, "main", False) for name in mains] or [
        (name, "main", others) for name in album_mains
    ]
    claims.extend(read)
    claims.extend((guest, "guest", False) for guest in featured)
    claims.extend((guest, "guest", True) for guest in guests_from_title(track.get("title") or ""))
    if classical:
        # The composer field's spelling ("Béla Bartók") over the line's ("Bela Bartok"); the line's when it has none.
        composer = str(safe_get(track, ["composer", "name"]) or "").strip()
        claims.extend((name, "composer", False) for name in ([composer] if composer else line_composers))
    return _credits_and_guesses(claims)


def _credits_and_guesses(claims: list[tuple[str, Role, bool]]) -> tuple[list[Credit], list[Credit]]:
    """Each credit once, every artist under the first spelling of their name, and the credits only guessed."""
    spelling: dict[str, str] = {}
    for name, _, _ in claims:
        spelling.setdefault(_key(name), name)
    credits: dict[Credit, bool] = {}  # each credit: whether only guessed
    for name, role, assumed in claims:
        credit = (spelling[_key(name)], role)
        credits[credit] = credits.get(credit, True) and assumed
    return list(credits), [credit for credit, guessed in credits.items() if guessed]
