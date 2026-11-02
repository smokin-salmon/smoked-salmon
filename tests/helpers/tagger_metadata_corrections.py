"""Helpers of tests/test_tagger_metadata_corrections.py that other test modules use too."""

from typing import Any


def _track(number: str, artists: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "track#": number,
        "disc#": "1",
        "tracktotal": "3",
        "disctotal": "1",
        "artists": artists,
        "title": f"Song {number}",
        "replay_gain": None,
        "peak": None,
        "isrc": None,
        "explicit": None,
        "format": None,
        "streamable": None,
    }


def _scraped(*tracks: list[tuple[str, str]]) -> dict[str, Any]:
    """A store's release, as a scraper returns it, with these artists on its tracks."""
    return {
        "artists": [],
        "title": "Album",
        "group_year": "2024",
        "year": "2024",
        "date": None,
        "edition_title": None,
        "label": None,
        "catno": None,
        "rls_type": "Album",
        "genres": ["Jazz"],
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
        "source": "WEB",
        "cover": None,
        "upc": None,
        "comment": None,
        "url": "https://www.qobuz.com/us-en/album/album/abc123",
        "urls": ["https://www.qobuz.com/us-en/album/album/abc123"],
        "tracks": {"1": {str(n): _track(str(n), artists) for n, artists in enumerate(tracks, 1)}},
    }
