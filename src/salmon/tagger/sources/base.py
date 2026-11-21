import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from itertools import chain
from typing import Any

from salmon import cfg
from salmon.common import RE_USELESS_VERSION, fetch_genre, less_uppers, normalize_accents, split_genre
from salmon.common.strings import join_artists, names_no_artist, reaches_various_threshold, unbracketed
from salmon.errors import GenreNotInWhitelist


class MetadataMixin(ABC):
    # These methods/attributes are expected to be provided by BaseScraper when used as a mixin
    format_url: Callable
    is_json_api: bool
    fetch_page: Callable  # HTML path: BaseScraper.fetch_page
    fetch_data: Callable  # JSON path: defined on JSON source bases

    # Whether the parser reads the store's credits as it states them (ADR 0007, step 3). Its tracks' credits are then
    # left as read, for the merge to decide, and any credit not marked a guess counts as the store's word. So the
    # parser marks every credit it guessed in the track's "assumed_artists", gives each name and role once on a track,
    # and writes each name as the store gives it, its guess marked under the same spelling.
    states_credits: bool = False

    async def scrape_release_from_id(self, rls_id: str) -> dict[str, Any]:
        """Run a scrape from the release ID."""
        return await self.scrape_release(self.format_url(rls_id=rls_id), rls_id=rls_id)

    async def scrape_release(self, url, rls_id=None):
        """
        Scrape the metadata of a release and return a dictionary of scraped data.
        Data may vary depending on the source; unavailable keys will be left
        as None.
        """
        if self.is_json_api:
            soup = await self.fetch_data(url, rls_id=rls_id)
        else:
            soup = await self.fetch_page(url)

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
        if self.states_credits:
            # The tracks' credits stay as read, for the merge to decide, and their titles as the store gives them,
            # remixes included; the release's credits, which the type and label are read from below, are built as for
            # any other store. generate_artists replaces each track's credits: on a copy of each track, the tracks
            # keep theirs.
            copied = {
                disc: {num: dict(track) for num, track in tracks.items()} for disc, tracks in data["tracks"].items()
            }
            data["artists"] = generate_artists(copied)[0]
        else:
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
        version: str | None = None,
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
        duration: int | None = None,
        credited_remixers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return a generated track dictionary containing the required values.

        Args:
            trackno: Track number.
            discno: Disc number.
            artists: List of (artist_name, role) tuples.
            title: Track title.
            version: The track's version, when the store gives it apart from the title ("PNAU Remix"): the merge
                decides how the two are written.
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
            duration: Track length in seconds, used to match local files to this
                tracklist when titles are ambiguous.
            credited_remixers: The remixers the store credits, by name, as against those read from a
                title: only these are written to the files' REMIXER tags.

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
            "version": version,
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
            "duration": duration,
            "credited_remixers": credited_remixers or [],
        }

    def determine_rls_type(self, data):
        def strip_base_title(title):
            return re.sub(r"\s*\(.*?\)", "", title).strip().lower()

        tracks = [track for disc in data["tracks"].values() for track in disc.values()]

        num_tracks = len(tracks)
        base_titles = {strip_base_title(title_with_version(track["title"], track.get("version"))) for track in tracks}
        main_artists = {artist[0] for artist in data.get("artists", []) if artist[1] == "main"}

        title = data["title"]
        rls_type = data.get("rls_type").lower() if data.get("rls_type") else ""

        # --- Title-based overrides ---
        if re.search(r"\bE\.?P\.?\b", title, re.IGNORECASE):
            return strip_ep_suffix(title), "EP"
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
        remix_count = sum(
            1
            for t in tracks
            if re.search(r"(mix|remix)", title_with_version(t["title"], t.get("version")), re.IGNORECASE)
        )
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
        """The title with its version, as the merge writes a store's (title_with_version), a featuring version kept:
        a store that folds its titles (Tidal) reads its guests from the title."""
        return title_with_version(title, version, keep_featuring=True)


def title_with_version(title, version, keep_featuring=False, keep_title_featuring=False):
    """A store's title written with its version, the one place the two become one string: by strip_useless_versions,
    a useless version is dropped ("Original Mix", "Remastered", a featuring clause either ends in, unless
    `keep_featuring` for the version's or `keep_title_featuring` for the title's), and one already in the title is not
    added again. folded_title, by contrast, joins them as the store does."""
    if strip := cfg.upload.formatting.strip_useless_versions:
        kept = keep_title_featuring and featuring_clause(title)
        base = title.strip() if kept else RE_USELESS_VERSION.sub("", title).strip()
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

    if strip and not keep_featuring and (version := unbracketed(version)) and featuring_clause(f"({version})"):
        version = None  # "ft. Mary J Blige": the guest credit says it.
    if version := unbracketed(version):
        # Already in the title's version part (from its first bracket or dash) from a word's start, brackets aside:
        # "Live" is in "Song (Live)", not in "Alive" nor "Live and Let Die", and "Remaster" in "(Remastered 2011)", as
        # "Live" is in "(Lively Mix)".
        plain = re.sub(r"[\(\)\[\]]", "", version).lower()
        said = re.search(r"[\(\[]|\s[-\u2013\u2014]\s", base)
        flat = re.sub(r"[\(\)\[\]]", "", base[said.start() :] if said else "").lower()
        if plain not in strip_set and not re.search(rf"(?<!\w){re.escape(plain)}", flat):
            # A version in brackets of its own at both ends ("(A) (B)") isn't wrapped in one more.
            base += f" {version}" if version[0] in "([" and version[-1] in ")]" else f" ({version})"
    return base


def featuring_clause(text):
    """The text before a featuring clause it ends in, and the names the clause gives: "Pre (feat. A & B)" gives
    ("Pre", "A & B"). None when it ends in none."""
    match = _RE_TRAILING_GROUP.search(text)
    feat = match and _RE_FEATURING_GROUP.match(match["group"])
    return (text[: match.start()].rstrip(), match["group"][feat.end() :].strip()) if match and feat else None


def folded_title(track) -> str | None:
    """The track's title with its version, as the store folds them on its own page ("Cold Heart (PNAU Remix)").

    What reads a store's title before the merge decides it (pairing, the release type, the merge's remixer read) reads
    this, so a version kept apart is still seen there.
    """
    title, version = track["title"], track.get("version")
    return f"{title} ({version})" if title and version else title


# What a store appends to a title to say the release is an EP: "Title - EP" (any dash, with a space before it, so
# "MAX-EP" stays a name), "Title (EP)", "Title [EP]".
RE_EP_STORE_SUFFIX = re.compile(
    r"(?:\s+[-‐-―]\s*E\.?P\.?|\s*\(\s*E\.?P\.?\s*\)|\s*\[\s*E\.?P\.?\s*\])\s*$", re.IGNORECASE
)
# "EP" ending the title itself: "Title EP".
RE_EP_TITLE_SUFFIX = re.compile(r"\s+E\.?P\.?\s*$", re.IGNORECASE)


def ends_with_ep(title):
    """Whether an EP marker ends the title, a store's or a bare "EP", whatever strip_ep_from_title says."""
    return bool(RE_EP_STORE_SUFFIX.search(title) or RE_EP_TITLE_SUFFIX.search(title))


def strip_ep_suffix(title):
    """
    Return the title without the EP marker that ends it.

    A store's marker ("Title - EP", "Title (EP)") always goes. A bare "EP" ending the
    title ("Title EP") goes only with strip_ep_from_title, as it is often part of the
    release's name. An "EP" inside the title ("The EP Sessions") is part of it and stays.
    """
    stripped = RE_EP_STORE_SUFFIX.sub("", title)
    if stripped == title and cfg.upload.formatting.strip_ep_from_title:
        stripped = RE_EP_TITLE_SUFFIX.sub("", title)
    return stripped.strip() or title


def label_names_a_main_artist(label: str, artists) -> bool:
    """Whether a label starts with a main artist's whole name: "Air Records" is Air's, "Airplane Records" isn't."""
    label = label.lower()
    # \b won't do: at "M.I.A.| Music" neither side is a word character. So a name ending in punctuation ends there,
    # whatever follows ("M.I.A.Music" too), and any other name needs no letter or digit after it.
    return any(i == "main" and re.match(re.escape(str(a).lower()) + r"(?:(?<=\W)|(?!\w))", label) for a, i in artists)


def determine_label_type(label, artists):
    """
    Determine the type of label based on the label and artists.
    Return "Self-Released" if self-released, otherwise return the original label.
    """
    if isinstance(label, str):
        # Check for "Not On Label" or "Self-Released" in the label
        if re.search(r"\b(?:not on label|no label|self[- ]?released)\b", label, re.IGNORECASE):
            return "Self-Released"

        if label_names_a_main_artist(label, artists):
            return "Self-Released"
    return label


def artist_spellings(tracks):
    """Each name's spelling on the release, by its accent-folded lower case: of two spellings, the one with fewer
    capitals ("Kool & the Gang" over "Kool & The Gang")."""
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
    artist_pool = artist_spellings(tracks)
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
    Takes a list of artist tuples, as [(artist, importance), ], and repairs
    badly split artists, such as Leslie Odom, Jr. (one artist) --> Leslie Odom
    / Jr. (two artists): two names whose stripped forms, joined either way,
    give a third name on the release are its parts. On each track, a role
    holding every part of a name gets the whole name in their place. With
    tracks, the release's artists are then the tracks' artists, as repaired.
    """
    to_replace = construct_replacement_list(artists)
    if tracks:
        artist_pool = artist_spellings(tracks)
        for _dnum, disc in tracks.items():
            for _tnum, track in disc.items():
                repaired = fix_artists_list(
                    list(
                        dict.fromkeys(
                            (artist_pool[normalize_accents(art.lower())], imp) for art, imp in track["artists"]
                        )
                    ),
                    to_replace,
                )
                # Then deduplicate, so a name joined back counts too. Main and guest, the ARTIST credit, are one credit
                # per name; another role (remixer, arranger, ...) is kept beside it.
                deduplicated_artists = []
                seen_normalized = set()

                for art, imp in repaired:
                    normalized_art = (normalize_accents(art.lower()), imp if imp not in {"main", "guest"} else "")
                    if normalized_art not in seen_normalized:
                        deduplicated_artists.append((art, imp))  # Add the original artist with the importance
                        seen_normalized.add(normalized_art)

                track["artists"] = deduplicated_artists
        # The release's artists are its tracks' artists, as repaired: a name joined on no track is joined nowhere.
        artists = list(
            dict.fromkeys(credit for disc in tracks.values() for track in disc.values() for credit in track["artists"])
        )
    else:
        artists = fix_artists_list(artists, to_replace)
    return artists, tracks


def construct_replacement_list(artists: list[tuple[str, str]]) -> list[tuple[list[str], str]]:
    """
    Create the list of artists-to-replace: each pair of names whose
    stripped forms, joined in either order, are another name's. Each name
    counts once, whatever its roles, and a name with no letters or digits
    is no part of another.

    Args:
        artists: List of (artist_name, importance) tuples.

    Returns:
        List of (replacements, replacement_artist) tuples.
    """
    to_replace: list[tuple[list[str], str]] = []
    # Build artist pool with normalized names
    unsorted_pool: list[list[str]] = []
    # Each name once, whatever its roles, so a name never pairs with itself; a name of punctuation only ("!!!") is
    # no part of another.
    for artist_name in dict.fromkeys(str(name) for name, _ in artists):
        stripped = "".join(s for s in artist_name if s.isalnum()).replace(" ", "")
        normalized_result = normalize_accents(stripped)
        # normalize_accents returns str when given single argument
        normalized = str(normalized_result).lower() if normalized_result else ""
        if normalized:
            unsorted_pool.append([normalized, artist_name])
    artist_pool: list[list[str]] = sorted(unsorted_pool, key=lambda x: len(x[0]))
    for i, pri_a_raw in enumerate(artist_pool):
        for other_a in reversed(artist_pool[0:i]):
            current_replacements = [pri_a_raw[1], other_a[1]]
            # Either part may come first: "Simon" and "Garfunkel", or "Leslie Odom" and "Jr.".
            joined = {other_a[0] + pri_a_raw[0], pri_a_raw[0] + other_a[0]}
            for artist_to_compare in artist_pool[i:]:
                if artist_to_compare[0] in joined:
                    to_replace.append((current_replacements, artist_to_compare[1]))
    return to_replace


def fix_artists_list(original_artists, to_replace):
    """
    Join each badly split name back: in a role where every part of it is
    credited, the parts go and the whole name is credited once, in the
    first place its parts or the whole held. A role holding only some of
    the parts ("Jr." alone, beside another name) is left as it is, and so
    is every other role.
    """
    for artist_type in dict.fromkeys(importa for _, importa in original_artists):
        for replaceds, replacement in to_replace:
            if not all((part, artist_type) in original_artists for part in replaceds):
                continue
            credits = [
                (name, artist_type) for name in (*replaceds, replacement) if (name, artist_type) in original_artists
            ]
            first = min(original_artists.index(credit) for credit in credits)
            for credit in credits:
                original_artists.remove(credit)
            original_artists.insert(first, (replacement, artist_type))

    return original_artists


# A whole (...) or [...] ending the title, and the credit a featuring group starts with ("feat" is not "Feather").
_RE_TRAILING_GROUP = re.compile(r"[(\[](?P<group>[^()\[\]]*)[)\]]\s*$")
_RE_FEATURING_GROUP = re.compile(r"\s*(?:featuring|feat|ft)(?:\.|\s|$)", flags=re.IGNORECASE)


def _ends_in_a_version(title):
    """Whether the title ends in a bracketed version of its own, as "(Blunt Edit)" or "[Club Dub]"."""
    match = _RE_TRAILING_GROUP.search(title)
    return bool(match) and not _RE_FEATURING_GROUP.match(match["group"])


def append_remixers_to_track_titles(data):
    for dnum, disc in data.items():
        for tnum, track in disc.items():
            # A "Various" remixer credit names nobody, and is dropped later (remove_various_artists).
            remix_artists = [a for a, i in track["artists"] if i == "remixer" and not names_no_artist(a)]
            if not any(x in track["title"] for x in ("Remix", "Mix")) and not _ends_in_a_version(track["title"]):
                if reaches_various_threshold(remix_artists):
                    data[dnum][tnum]["title"] += " (Remixed)"
                elif remix_artists:
                    data[dnum][tnum]["title"] += f" ({join_artists(remix_artists)} Remix)"

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


def standardize_genres(genres):
    # Order follows the input: a set here would make the result depend on the hash seed.
    ordered = []
    for g in genres:
        for part in split_genre(g):
            try:
                resolved = sorted(fetch_genre(part))
            except GenreNotInWhitelist:
                resolved = [part]
            for item in resolved:
                if item not in ordered:
                    ordered.append(item)

    # Filter out generic genres if more specific combos exist
    return [g for g in ordered if not any(g != o and _is_separate_word_in_combination(g, o) for o in ordered)]
