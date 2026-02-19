import asyncio
import re
from itertools import chain
from typing import Any

import asyncclick as click

from salmon import cfg
from salmon.common import (
    commandgroup,
    handle_scrape_errors,
    normalize_accents,
    re_split,
    re_strip,
)
from salmon.search import (
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

SEARCHSOURCES = {
    "Bandcamp": bandcamp,
    "MusicBrainz": musicbrainz,
    "iTunes": itunes,
    "Junodownload": junodownload,
    "Discogs": discogs,
    "Beatport": beatport,
    "Qobuz": qobuz,
    "Tidal": tidal,
    "Deezer": deezer,
}


@commandgroup.command()
@click.argument("searchstr", nargs=-1, required=True)
@click.option("--track-count", "-t", type=click.INT)
@click.option("--limit", "-l", type=click.INT, default=cfg.upload.search.limit)
async def metas(searchstr: tuple[str, ...], track_count: int | None, limit: int) -> None:
    """Search for releases from metadata providers."""
    search_query = " ".join(searchstr)
    click.secho(f"Searching {', '.join(SEARCHSOURCES)} (searchstrs: {search_query})", fg="cyan", bold=True)

    results = await run_metasearch([search_query], limit=limit, track_count=track_count)
    not_found: list[str] = []
    inactive_sources: list[str] = []
    source_errors = set(SEARCHSOURCES.keys()) - set(results)
    for source, releases in results.items():
        if releases:
            click.secho(f"\nResults from {source}:", fg="yellow", bold=True)
            for rls_id, release in releases.items():
                rls_name = release[0][1]
                url = SEARCHSOURCES[source].Searcher.format_url(rls_id, rls_name)
                click.echo(f"> {release[1]} {url}")
        elif source:
            if releases is None:
                inactive_sources.append(source)
            else:
                not_found.append(source)

    click.echo()
    for source in not_found:
        click.secho(f"No results found from {source}.", fg="red")
    for source in inactive_sources:
        click.secho(
            f"{source} is inactive. Update your config.py with the necessary tokens if you want to enable it.",
            fg="red",
        )
    if source_errors:
        click.secho(f"Failed to scrape {', '.join(source_errors)}.", fg="red")


async def run_metasearch(
    searchstrs: list[str],
    limit: int = cfg.upload.search.limit,
    sources: dict[str, Any] | None = None,
    track_count: int | None = None,
    artists: list[str] | None = None,
    album: str | None = None,
    filter: bool = True,
) -> dict[str, Any]:
    """Run a search for releases matching the searchstr.

    Args:
        searchstrs: List of search strings.
        limit: Maximum number of results per source.
        sources: Dict of sources to search, defaults to all.
        track_count: Filter by track count if specified.
        artists: Filter by artists if specified.
        album: Filter by album name if specified.
        filter: Whether to apply filtering.

    Returns:
        Dict mapping source names to search results.
    """
    sources = SEARCHSOURCES if not sources else {k: m for k, m in SEARCHSOURCES.items() if k in sources}
    results: dict[str, Any] = {}
    tasks = [
        handle_scrape_errors(s.Searcher().search_releases(search, limit))
        for search in searchstrs
        for s in sources.values()
    ]
    task_responses = await asyncio.gather(*tasks)
    for source, result in [r or (None, None) for r in task_responses]:
        if result:
            if filter:
                result = filter_results(result, artists, album)
            if track_count:
                result = filter_by_track_count(result, track_count)
        if source:
            results[source] = result
    return results


def filter_results(
    results: dict[str, Any] | None,
    artists: list[str] | None,
    album: str | None,
) -> dict[str, Any]:
    """Filter search results by artist and album.

    Args:
        results: Search results to filter.
        artists: Artist names to match.
        album: Album name to match.

    Returns:
        Filtered results dict.
    """
    filtered: dict[str, Any] = {}
    for rls_id, result in (results or {}).items():
        if artists:
            split_artists: list[str] = []
            for a in artists:
                split_artists += re_split(re_strip(normalize_accents(a)))
            stripped_rls_artist = re_strip(normalize_accents(result[0].artist))

            if "Various" in result[0].artist:
                if len(artists) == 1:
                    continue
            elif not any(a in stripped_rls_artist for a in split_artists) or not any(
                a in stripped_rls_artist.split() for a in chain.from_iterable([a.split() for a in split_artists])
            ):
                continue
        if album and not _compare_albums(album, result[0].album):
            continue
        filtered[rls_id] = result
    return filtered


def filter_by_track_count(results: dict[str, Any], track_count: int) -> dict[str, Any]:
    """Filter results by track count.

    Args:
        results: Search results to filter.
        track_count: Expected track count.

    Returns:
        Filtered results dict.
    """
    filtered: dict[str, Any] = {}
    for rls_id, (ident_data, res_str) in results.items():
        if not ident_data.track_count or abs(ident_data.track_count - track_count) <= 1:
            filtered[rls_id] = (ident_data, res_str)
    return filtered


def _compare_albums(one: str, two: str) -> bool:
    """Compare two album names for similarity.

    Args:
        one: First album name.
        two: Second album name.

    Returns:
        True if albums are considered similar.
    """
    one, two = normalize_accents(one, two)
    regex_pattern = r" \(?(mix|feat|with|incl|prod).+"
    return bool(
        re_strip(one) == re_strip(two)
        or re_strip(re.sub(regex_pattern, "", one, flags=re.IGNORECASE))
        == re_strip(re.sub(regex_pattern, "", two, flags=re.IGNORECASE))
    )
