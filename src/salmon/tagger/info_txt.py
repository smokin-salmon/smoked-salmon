"""Parse an ``info.txt`` file placed in an album folder into release metadata.

The returned dict matches the scraper schema (``MetadataMixin.scrape_release``),
minus ``url`` (info.txt is not a scraped source).
"""

import re
from collections import defaultdict
from pathlib import Path

from salmon.constants import ARTIST_IMPORTANCES
from salmon.tagger.sources.base import assign_track_totals, generate_artists, standardize_genres

INFO_TXT_FILENAME = "info.txt"

KEY_ALIASES = {
    "title": {"title", "release title", "album", "album title"},
    "artists": {"artist", "artists", "album artist", "album artists"},
    "label": {"label", "record label"},
    "catno": {"catno", "catalog number", "catalog #", "catalog no", "catalog no.", "catalog"},
    "year": {"year", "release year", "date", "group year"},
    "genres": {"genre", "genres"},
    "upc": {"upc", "barcode", "ean"},
    "edition_title": {"edition", "edition title", "version"},
    "rls_type": {"type", "release type", "rls type", "rls_type"},
    "cover": {"cover", "url"},
    "comment": {"comment"},
    "source": {"source", "format"},
    "composer": {"composer"},
    "conductor": {"conductor"},
}
_ALIAS_TO_CANONICAL = {alias: canon for canon, aliases in KEY_ALIASES.items() for alias in aliases}

_DISC_HEADER_RE = re.compile(r"^(cd|disc)\s*0*(\d+)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\d{4}")
_TRACKNUM_RE = re.compile(r"^\s*(\d+)\s*(?:[.):]|--|\u2014|\u2013|-)?\s+")
_SEPARATOR_RE = re.compile(r" - |--|\u2014|\u2013")
_ROLE_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")
_ARTIST_SPLIT_RE = re.compile(r" \\ |/|; | & |, ")
_GENRE_SPLIT_RE = re.compile(r"[,;/]")
_VARIOUS = {"various", "various artists"}


def find_info_txt(path: str) -> str | None:
    """Return the path to the ``info.txt`` file in an album folder, if present.

    The match is case-sensitive: ``Info.txt`` is not found.
    """
    candidate = Path(path) / INFO_TXT_FILENAME
    if candidate.is_file():
        return str(candidate)
    return None


def _read_text(info_path: str) -> str:
    data = Path(info_path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "latin1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError(f"Could not decode {info_path}")  # latin1 never fails; unreachable


def _is_comment_or_blank(stripped: str) -> bool:
    return not stripped or stripped.startswith("#") or stripped.startswith("//")


def _parse_artists(artist_part: str) -> list[tuple[str, str]]:
    """Split a multi-artist string; explicit ``Name (role)`` overrides default main."""
    artists = []
    for part in _ARTIST_SPLIT_RE.split(artist_part):
        part = part.strip()
        if not part:
            continue
        role_match = _ROLE_RE.match(part)
        if role_match:
            name = role_match.group(1).strip()
            role = role_match.group(2).strip().lower()
            if role not in ARTIST_IMPORTANCES:
                role = "main"
            if not name:
                continue
            artists.append((name, role))
        else:
            artists.append((part, "main"))
    return artists


def _parse_track_line(line: str) -> tuple[str | None, list[tuple[str, str]] | None, str | None]:
    """Parse an ``Artist - Title`` line; returns (number, artists, title).

    Returns (None, None, None) when the line is not a track.
    """
    num_match = _TRACKNUM_RE.match(line)
    if num_match:
        num = str(int(num_match.group(1)))
        rest = line[num_match.end() :]
    else:
        num = None
        rest = line
    sep_match = _SEPARATOR_RE.search(rest)
    if not sep_match:
        return None, None, None
    artist_part = rest[: sep_match.start()].strip()
    title = rest[sep_match.end() :].strip()
    if not artist_part or not title:
        return None, None, None
    return num, _parse_artists(artist_part), title


def _make_track(num: str, disc: str, artists: list[tuple[str, str]], title: str) -> dict:
    return {
        "track#": num,
        "disc#": disc,
        "tracktotal": None,
        "disctotal": None,
        "artists": artists,
        "title": title,
        "replay_gain": None,
        "peak": None,
        "explicit": None,
        "isrc": None,
        "format": None,
        "stream_id": None,
        "streamable": None,
        "md5_origin": None,
        "media_version": None,
        "lossless": None,
        "mp3_320": None,
    }


def parse_info_txt(info_path: str) -> dict:
    """Parse an info.txt file into a scraper-schema metadata dict (no ``url``)."""
    lines = _read_text(info_path).splitlines()

    headers: dict[str, str] = {}
    genre_parts: list[str] = []
    tracks: dict[str, dict[str, dict]] = {}
    auto_counters: dict[str, int] = defaultdict(lambda: 1)
    current_disc = "1"
    tracks.setdefault(current_disc, {})

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        i += 1
        if _is_comment_or_blank(stripped):
            continue

        disc_match = _DISC_HEADER_RE.match(stripped)
        if disc_match:
            current_disc = str(int(disc_match.group(2)))
            tracks.setdefault(current_disc, {})
            continue

        canonical = None
        value = None
        if ":" in stripped:
            key, _, after = stripped.partition(":")
            if key.strip().lower() in _ALIAS_TO_CANONICAL and after.strip():
                canonical = _ALIAS_TO_CANONICAL[key.strip().lower()]
                value = after.strip()
        if canonical is None:
            bare = stripped[:-1].strip() if stripped.endswith(":") else stripped
            if (
                bare.lower() in _ALIAS_TO_CANONICAL
                and not _SEPARATOR_RE.search(stripped)
                and i < len(lines)
                and lines[i][:1] in (" ", "\t")
                and not _is_comment_or_blank(lines[i].strip())
            ):
                canonical = _ALIAS_TO_CANONICAL[bare.lower()]
                value = lines[i].strip()
                i += 1
        if canonical is not None:
            if not value:
                continue
            if canonical == "genres":
                genre_parts.append(value)
            else:
                headers[canonical] = value
            continue

        num, artists, title = _parse_track_line(line)
        if artists is None or title is None:
            continue
        disc_tracks = tracks[current_disc]
        if num is None or num in disc_tracks:
            num = str(auto_counters[current_disc])
            while num in disc_tracks:
                auto_counters[current_disc] += 1
                num = str(auto_counters[current_disc])
            auto_counters[current_disc] += 1
        disc_tracks[num] = _make_track(num, current_disc, artists, title)

    def _clean(val: str | None) -> str | None:
        val = (val or "").strip()
        return val or None

    def _clean_na(val: str | None) -> str | None:
        val = _clean(val)
        if val is not None and val.lower() == "n/a":
            return None
        return val

    year = None
    if headers.get("year"):
        year_match = _YEAR_RE.search(headers["year"])
        if year_match:
            year = int(year_match.group(0))

    genres = [g.strip() for part in genre_parts for g in _GENRE_SPLIT_RE.split(part)]
    genres = [g for g in genres if g]

    global_composers = (
        [(n, "composer") for n, _ in _parse_artists(headers["composer"])] if headers.get("composer") else []
    )
    global_conductors = (
        [(n, "conductor") for n, _ in _parse_artists(headers["conductor"])] if headers.get("conductor") else []
    )

    if tracks and any(tracks.values()):
        for disc_tracks in tracks.values():
            for track in disc_tracks.values():
                roles = {role for _, role in track["artists"]}
                if global_composers and "composer" not in roles:
                    track["artists"].extend(global_composers)
                if global_conductors and "conductor" not in roles:
                    track["artists"].extend(global_conductors)
                track["artists"] = [(n, r) for n, r in track["artists"] if n.strip().lower() not in _VARIOUS]
        tracks = {d: t for d, t in tracks.items() if t}
        tracks = assign_track_totals(tracks)
        album_artists, tracks = generate_artists(tracks)
    else:
        tracks = {}
        album_artists = _parse_artists(headers["artists"]) if headers.get("artists") else []

    return {
        "title": _clean(headers.get("title")),
        "cover": _clean(headers.get("cover")),
        "genres": standardize_genres(genres),
        "year": year,
        "group_year": year,
        "date": None,
        "edition_title": _clean(headers.get("edition_title")),
        "label": _clean(headers.get("label")),
        "catno": _clean_na(headers.get("catno")),
        "rls_type": _clean(headers.get("rls_type")) or "Album",
        "tracks": tracks,
        "upc": _clean_na(headers.get("upc")),
        "comment": _clean(headers.get("comment")),
        "scene": False,
        "encoding": None,
        "media": None,
        "source": _clean(headers.get("source")),
        "urls": [],
        "artists": album_artists,
    }
