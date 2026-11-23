"""Deezer's answers as its parser reads them."""

import anyio

from salmon.tagger.sources.deezer import Scraper


def deezer_song(number: int, version: str = "", title: str = "Spellbound", contributors: dict | None = None) -> dict:
    """A track of Deezer's album page, its contributors Alice as main unless given."""
    return {
        "DISK_NUMBER": "1",
        "TRACK_NUMBER": str(number),
        "SNG_TITLE": title,
        "VERSION": version,
        "SNG_CONTRIBUTORS": {"main_artist": ["Alice"]} if contributors is None else contributors,
        "ARTISTS": [],
        "ISRC": None,
        "EXPLICIT_LYRICS": "0",
        "SNG_ID": str(number),
    }


def deezer_tracks(*songs: dict) -> dict:
    """The tracks Deezer's parser makes of these songs."""
    return anyio.run(Scraper().parse_tracks, {"tracklist": list(songs)})
