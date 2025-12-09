from typing import Any

import asyncclick as click

from salmon.errors import ScrapeError
from salmon.tagger.sources import (
    bandcamp,
    beatport,
    deezer,
    discogs,
    itunes,
    junodownload,
    musicbrainz,
    qobuz,
    tidal,
)

METASOURCES = {
    "MusicBrainz": musicbrainz,
    "iTunes": itunes,
    "Junodownload": junodownload,
    "Deezer": deezer,
    "Discogs": discogs,
    "Beatport": beatport,
    "Qobuz": qobuz,
    "Tidal": tidal,
    "Bandcamp": bandcamp,  # Must be last due to the catch-all nature of its URLs.
}


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
            if return_source_name:
                return await source.Scraper().scrape_release(url), name
            return await source.Scraper().scrape_release(url)
    raise ScrapeError("URL did not match a scraper.")
