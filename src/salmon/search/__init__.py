import asyncio
import re
from collections.abc import Callable, Iterable
from itertools import chain, zip_longest
from operator import itemgetter
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
from salmon.common.strings import names_no_artist
from salmon.search import (
    apple_music,
    bandcamp,
    beatport,
    deezer,
    discogs,
    musicbrainz,
    qobuz,
    tidal,
)

SEARCHSOURCES = {
    "Bandcamp": bandcamp,
    "MusicBrainz": musicbrainz,
    "Apple Music": apple_music,
    "Discogs": discogs,
    "Beatport": beatport,
    "Qobuz": qobuz,
    "Tidal": tidal,
    "Deezer": deezer,
}


def searched_sources() -> dict[str, Any]:
    """The stores salmon searches: every one but those in upload.search.skipped_sources."""
    return {name: module for name, module in SEARCHSOURCES.items() if name not in cfg.upload.search.skipped_sources}


def listing_order(names: Iterable[str]) -> list[str]:
    """`names` in the order salmon lists store results: those in upload.search.preferred_sources first, in its order,
    then the others in the order given."""
    names = list(names)
    preferred = [name for name in cfg.upload.search.preferred_sources if name in names]
    return preferred + [name for name in names if name not in preferred]


def store_heading(name: str) -> str:
    """The store's name as a heading over its results: marked when it is in upload.search.preferred_sources."""
    return f"{name} (preferred)" if name in cfg.upload.search.preferred_sources else name


@commandgroup.command()
@click.argument("searchstr", nargs=-1, required=True)
@click.option("--track-count", "-t", type=click.INT)
@click.option("--limit", "-l", type=click.INT, default=cfg.upload.search.limit)
async def metas(searchstr: tuple[str, ...], track_count: int | None, limit: int) -> None:
    """Search for releases from metadata providers."""
    search_query = " ".join(searchstr)
    searched = searched_sources()
    click.secho(f"Searching {', '.join(searched)} (searchstrs: {search_query})", fg="cyan", bold=True)

    results = await run_metasearch([search_query], limit=limit, track_count=track_count)
    not_found: list[str] = []
    inactive_sources: list[str] = []
    source_errors = set(searched) - set(results)
    for source in listing_order(results):
        releases = results[source]
        if releases:
            click.secho(f"\nResults from {store_heading(source)}:", fg="yellow", bold=True)
            for rls_id, release in releases.items():
                rls_name = release[0].album
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
        sources: Dict of sources to search, defaults to all but upload.search.skipped_sources.
        track_count: Filter by track count if specified.
        artists: Filter by artists if specified.
        album: Filter by album name if specified.
        filter: Whether to apply filtering.

    Returns:
        Dict mapping source names to search results.
    """
    sources = searched_sources() if not sources else {k: m for k, m in SEARCHSOURCES.items() if k in sources}
    answers: dict[str, list[Any]] = {}
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
            answers.setdefault(source, []).append(result)
    return {source: _merge_answers(source, found) for source, found in answers.items()}


# A Tidal result's key holds the region that found it, and two searches can find one release from different regions.
_SAME_RELEASE: dict[str, Callable[[Any], Any]] = {"Tidal": itemgetter(1)}


def _merge_answers(source: str, answers: list[dict[Any, Any] | None]) -> dict[Any, Any] | None:
    """One store's answers to its searches, each search's best first, so that a list cut to the limit holds some of
    each. A release found twice keeps the first search's entry.

    An answer is None when the store is inactive and empty when it found nothing: found beats empty.
    """
    found = [answer for answer in answers if answer]
    if not found:
        return answers[0]  # Empty, or None for an inactive store: the same for every search.
    release_of = _SAME_RELEASE.get(source, lambda rls_id: rls_id)
    first_entries: dict[Any, tuple[Any, Any]] = {}
    for answer in found:
        for rls_id, release in answer.items():
            first_entries.setdefault(release_of(rls_id), (rls_id, release))
    return dict(
        first_entries[release_of(rls_id)] for rank in zip_longest(*found) for rls_id in rank if rls_id is not None
    )


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

            if names_no_artist(result[0].artist):
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
