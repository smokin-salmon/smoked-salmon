"""What the files say about their release, for a store that can search by field (MusicBrainz, #1178).

The rules are Picard 3.0.5's cluster lookup, which the fielded search was measured with: the album artist and album
are the files' most common values, and the date counts only when most files carry it and all of them agree.
"""

import re
from collections import Counter
from collections.abc import Mapping
from typing import Any

import msgspec

VARIOUS_ARTISTS = "Various Artists"
_NON_WORD = re.compile(r"\W", re.UNICODE)
_SPACE = re.compile(r"\s", re.UNICODE)
_YEAR = re.compile(r"\d{4}")


class SearchHints(msgspec.Struct, frozen=True):
    """The files' album artist, album, track count and year; None where the tags don't settle it."""

    artist: str | None
    album: str | None
    track_count: int | None
    year: str | None


def _text(value: Any) -> str:
    """A tag's value as one string: a list's values joined with "; ", as TagFile joins them; "" when absent."""
    if isinstance(value, list | tuple):
        return "; ".join(str(v) for v in value if str(v))
    return str(value) if value else ""


def _token(name: str) -> str:
    """The name lower-cased without its non-word characters, so "AC/DC" and "ac dc" count as one artist."""
    return _NON_WORD.sub("", name.lower()) or _SPACE.sub("", name.lower())


def _artist(files: list[Any]) -> str | None:
    spellings: dict[str, Counter[str]] = {}
    for file in files:
        name = _text(file.albumartist) or _text(file.artist) or VARIOUS_ARTISTS
        spellings.setdefault(_token(name), Counter())[name] += 1
    if not spellings:
        return None
    group = max(spellings.values(), key=lambda counts: counts.total())
    return group.most_common(1)[0][0]


def _album(files: list[Any]) -> str | None:
    albums = Counter(album for file in files if (album := _text(file.album)))
    return albums.most_common(1)[0][0] if albums else None


def _year(files: list[Any]) -> str | None:
    dates = [date for file in files if (date := _text(file.date))]
    if len(dates) <= len(files) / 2 or len(set(dates)) != 1:
        return None
    match = _YEAR.match(dates[0])
    return match.group() if match else None


def search_hints(tags: Mapping[str, Any]) -> SearchHints:
    """The hints in the files' tags ({filename: TagFile}, as get_metadata has them)."""
    files = list(tags.values())
    return SearchHints(_artist(files), _album(files), len(files) or None, _year(files))
