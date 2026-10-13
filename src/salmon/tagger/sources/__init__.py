from typing import Any

import asyncclick as click

from salmon.errors import ScrapeError
from salmon.tagger.sources import (
    apple_music,
    bandcamp,
    beatport,
    deezer,
    discogs,
    musicbrainz,
    qobuz,
    tidal,
)

METASOURCES = {
    "MusicBrainz": musicbrainz,
    "Apple Music": apple_music,
    "Deezer": deezer,
    "Discogs": discogs,
    "Beatport": beatport,
    "Qobuz": qobuz,
    "Tidal": tidal,
    "Bandcamp": bandcamp,  # Must be last due to the catch-all nature of its URLs.
}

# What `salmon meta` leaves out of a release's metadata: the files' properties, which no store knows.
NOT_SHOWN = ("encoding", "media", "encoding_vbr", "source")


def matching_source(url: str) -> str | None:
    """The name of the metadata source whose scraper takes `url`, as run_metadata picks it; None for none."""
    return next((name for name, source in METASOURCES.items() if source.Scraper.regex.match(url)), None)


def _without_artist_variations(metadata: dict[str, Any]) -> dict[str, Any]:
    """Drop what a scraper keeps beside each track for the merge with file tags (Discogs ANVs).

    Whoever shows or uses a scrape on its own has no tags to match, so it should not see it either."""
    for disc in metadata["tracks"].values():
        for track in disc.values():
            track.pop("artist_variations", None)
    return metadata


async def run_metadata(
    url: str,
    sources: dict[str, Any] | None = None,
    return_source_name: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], str]:
    """Run a scrape for the metadata of a URL.

    Args:
        url: The URL to scrape metadata from.
        sources: Optional dict of sources to use, defaults to all.
        return_source_name: If True, return tuple of (metadata, source_name).

    Returns:
        Metadata dict, or tuple of (metadata, source_name) if return_source_name is True.

    Raises:
        ScrapeError: If URL doesn't match any scraper.
    """
    sources = METASOURCES if not sources else {name: source for name, source in METASOURCES.items() if name in sources}
    for name, source in sources.items():
        if source.Scraper.regex.match(url):
            click.secho(f"Getting metadata from {name}.", fg="cyan")
            metadata = _without_artist_variations(await source.Scraper().scrape_release(url))
            return (metadata, name) if return_source_name else metadata
    raise ScrapeError("URL did not match a scraper.")
