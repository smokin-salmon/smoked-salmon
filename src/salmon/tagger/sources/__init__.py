from typing import Any
from urllib.parse import urlsplit

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


def without_tracking(url: str) -> str:
    """A store link without its query and fragment (tracking, share IDs); any other link as given."""
    source = matching_source(url)
    if source is None:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:  # An unclosed "[" in the host.
        return url
    host = parts.hostname or ""
    # Bandcamp's pattern takes any host (artists' own domains), where a query may name the release.
    if source == "Bandcamp" and host != "bandcamp.com" and not host.endswith(".bandcamp.com"):
        return url
    bare = parts._replace(query="", fragment="").geturl()
    return bare if matching_source(bare) == source else url


# What a scraper keeps beside each track for the merge alone: Discogs' credited names (ANVs), a stating store's
# guesses, a version given apart from the title, and the stores' titles the merge gathers (with the store that named a
# placeholder). The merge drops them once it has decided, and a scrape shown on its own never holds them.
MERGE_ONLY_KEYS = ("artist_variations", "assumed_artists", "version", "store_titles", "placeholder_from")


def without_merge_only_keys(metadata: dict[str, Any]) -> None:
    """Drop MERGE_ONLY_KEYS from each track."""
    for disc in metadata["tracks"].values():
        for track in disc.values():
            for key in MERGE_ONLY_KEYS:
                track.pop(key, None)


async def run_metadata(
    url: str,
    sources: dict[str, Any] | None = None,
    return_source_name: bool = False,
    for_merge: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], str]:
    """Run a scrape for the metadata of a URL: used on its own, its credits decided as a merge of one source decides
    them and MERGE_ONLY_KEYS dropped.

    Args:
        url: The URL to scrape metadata from.
        sources: Optional dict of sources to use, defaults to all.
        return_source_name: If True, return tuple of (metadata, source_name).
        for_merge: If True, the scrape is left undecided and keeps MERGE_ONLY_KEYS, for combine_metadatas.

    Returns:
        Metadata dict, or tuple of (metadata, source_name) if return_source_name is True.

    Raises:
        ScrapeError: If URL doesn't match any scraper.
    """
    sources = METASOURCES if not sources else {name: source for name, source in METASOURCES.items() if name in sources}
    for name, source in sources.items():
        if source.Scraper.regex.match(url):
            click.secho(f"Getting metadata from {name}.", fg="cyan")
            metadata = await source.Scraper().scrape_release(url)
            if not for_merge:
                from salmon.tagger.combine import decide_alone  # combine imports this module

                decide_alone(metadata, name)
                without_merge_only_keys(metadata)
            return (metadata, name) if return_source_name else metadata
    raise ScrapeError("URL did not match a scraper.")
