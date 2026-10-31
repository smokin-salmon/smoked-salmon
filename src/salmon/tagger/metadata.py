import asyncio
import json
import re
from copy import copy, deepcopy
from dataclasses import dataclass
from itertools import islice
from typing import Any

import asyncclick as click
import msgspec

from salmon import cfg, interaction
from salmon.checks.source import is_store_url
from salmon.common import handle_scrape_errors, make_searchstrs, re_strip
from salmon.common.strings import artist_keys, comparable
from salmon.search import SEARCHSOURCES, listing_order, run_metasearch, searched_sources, store_heading
from salmon.search.base import IdentData
from salmon.sources.deezer import DeezerBase, album_upc
from salmon.tagger.combine import combine_metadatas
from salmon.tagger.sources import METASOURCES
from salmon.tagger.sources.base import generate_artists, standardize_genres
from salmon.tagger.tag_urls import tag_urls


@dataclass
class MetadataSearch:
    """A store search, kept so that its results can be picked from again with no second search."""

    # The stores' results; None: no search ran (--skip-search took its URL).
    results: dict[str, Any] | None = None
    # A deep copy of rls_data from before any pick, which writes into rls_data.
    base: dict[str, Any] | None = None


async def get_metadata(
    path: str,
    tags: dict[str, Any],
    rls_data: dict[str, Any],
    *,
    skip_search_url: str | None = None,
    skip_search: bool = False,
    search: MetadataSearch | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Get metadata pertaining to a release from various metadata sources.

    Have the user decide which sources to use, and then combine their information.

    Args:
        path: Path to the album folder.
        tags: Tag data from audio files.
        rls_data: Release data dictionary.
        skip_search_url: The --source-url the user gave.
        skip_search: --skip-search: take the metadata from skip_search_url without searching the stores.
        search: Filled with the store search, when one runs, so that pick_metadata can pick from it again.

    Returns:
        Tuple of (metadata dict, source URL or None).
    """
    click.secho("\nChecking metadata...", fg="cyan", bold=True)
    if not rls_data:
        raise ValueError("rls_data cannot be None")
    if skip_search:
        scraped = await _metadata_from_source_url(skip_search_url, rls_data)
        if scraped is not None:
            metadata, source_url = scraped
            return await _finish_metadata(metadata, path), source_url
    searchstrs = make_searchstrs(rls_data["artists"], rls_data["title"])
    click.secho(f"Searching for '{searchstrs}' releases...")
    artists_list = [a for a, _ in rls_data["artists"]]
    album_title = rls_data["title"]
    if search is None:
        search = MetadataSearch()
    search.results = await run_metasearch(
        searchstrs, filter=False, track_count=len(tags), artists=artists_list, album=album_title
    )
    search.base = deepcopy(rls_data)
    return await pick_metadata(search, rls_data, path, len(tags))


async def pick_metadata(
    search: MetadataSearch, rls_data: dict[str, Any], path: str, track_count: int
) -> tuple[dict[str, Any], str | None]:
    """Ask which of the search's results to use, from rls_data as it was before any pick: no new search.

    rls_data is restored in place, as the caller holds it. Returns the metadata and the source URL, as get_metadata.
    """
    assert search.results is not None and search.base is not None  # Type narrowing: a search ran
    rls_data.clear()
    rls_data.update(deepcopy(search.base))
    choices = _print_search_results(search.results, rls_data)
    default = suggest_choice(choices, search.results, rls_data, track_count, files_store_url(path))
    _explain_default(default, choices)
    metadata, source_url = await _select_choice(choices, rls_data, default=default)
    return await _finish_metadata(metadata, path), source_url


def _explain_default(default: str | None, choices: dict[int, tuple[str, str]]) -> None:
    """Say where each part of the pre-typed metadata answer came from."""
    for part in (default or "").split():
        if part.startswith("*"):
            click.secho(f"Pre-typed {part}: the store page in the files' tags, starred as the source.", fg="cyan")
        elif part.isdigit() and int(part) in choices:
            source = choices[int(part)][0]
            click.secho(
                f"Pre-typed {part}: the {source} result matching the files' artist, title and track count.",
                fg="cyan",
            )


async def _finish_metadata(metadata: dict[str, Any], path: str) -> dict[str, Any]:
    await fill_upc_from_deezer(metadata, path)
    _dedupe_catno_against_upc(metadata)
    remove_various_artists(metadata)
    return fix_hardcore_genre(metadata)


async def _metadata_from_source_url(url: str | None, rls_data: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """--skip-search: the metadata of the --source-url, scraped as a URL pasted at the prompt is; None to search.

    Says why in yellow when the search runs anyway because there is no URL a metadata source scrapes, and in red
    when the scrape fails.
    """
    url = (url or "").strip()
    if not url:
        click.secho("--skip-search needs --source-url: searching the stores anyway.", fg="yellow")
        return None
    name = _metasource_of(url)
    if name is None:
        click.secho(f"--skip-search: no metadata source scrapes {url}: searching the stores anyway.", fg="yellow")
        return None
    click.secho(f"Skipping the search: taking the metadata from {url}", fg="cyan")
    rls_data.setdefault("urls", [])
    added = url not in rls_data["urls"]
    if added:
        rls_data["urls"].append(url)
    scraped = await handle_scrape_errors(METASOURCES[name].Scraper().scrape_release(url))
    if not scraped:
        if added:
            rls_data["urls"].remove(url)
        click.secho("Could not take the metadata from that URL: searching the stores instead.", fg="yellow")
        return None
    return _combine_scraped([name], [scraped], rls_data, url), url


def _combine_scraped(
    sources: list[str], metadatas: list[Any], rls_data: dict[str, Any], source_url: str | None
) -> dict[str, Any]:
    """The scraped metadatas, combined over the tags' own and cleaned."""
    meta = combine_metadatas(
        *((s, m) for s, m in zip(sources, metadatas, strict=False) if m), base=rls_data, source_url=source_url
    )
    meta = clean_metadata(meta)
    meta["artists"], meta["tracks"] = generate_artists(meta["tracks"])
    return meta


def _deezer_album_ids(urls: list[str]) -> set[str]:
    """The distinct Deezer album IDs among `urls`; one album in several URL forms is one ID."""
    ids = set()
    for url in urls:
        match = DeezerBase.regex.search(url)
        if match and match[1] == "album":
            ids.add(match[2])
    return ids


async def fill_upc_from_deezer(metadata: dict[str, Any], path: str) -> None:
    """Take a missing UPC from the files' own Deezer album; another store's release may carry a different barcode.

    Makes one request at most, and only when the UPC is still empty and the files' tags name exactly one
    Deezer album: with two different albums (a compilation, a mistagged track) any pick could be the wrong barcode.
    """
    if metadata.get("upc"):
        return
    sourced, other = tag_urls(path)
    ids = _deezer_album_ids([*sourced, *other])
    if len(ids) != 1:
        return
    metadata["upc"] = await album_upc(f"https://www.deezer.com/album/{ids.pop()}")


class _Choice(tuple[str, str]):
    """A numbered search result: (source, release ID), plus the release the search found (its IdentData), which
    the metadata question describes for salmon web."""

    ident: IdentData | None

    def __new__(cls, source: str, rls_id: str, ident: IdentData | None = None) -> "_Choice":
        choice = super().__new__(cls, (source, rls_id))
        choice.ident = ident
        return choice


def _print_search_results(results, rls_data=None):
    """Print the results from the metadata source.

    The stores in upload.search.preferred_sources come first, and the results are numbered in the order printed.
    """
    if rls_data:
        _print_metadata(rls_data, metadata_name="Previous")

    choices = {}
    choice_id = 1
    searched = searched_sources()
    not_found = list(searched.keys())
    inactive_sources = []
    source_errors = searched.keys() - [r for r in results]

    for source in listing_order(results):
        releases = results[source]
        if releases:
            click.secho(f"\nResults for {store_heading(source)}:", fg="yellow", bold=True)
            not_found.remove(source)
            shown = dict(islice(releases.items(), cfg.upload.search.limit))
            for rls_id, release in shown.items():
                choices[choice_id] = _Choice(source, rls_id, release[0])
                url = SEARCHSOURCES[source].Searcher.format_url(rls_id)
                click.secho(f"> {choice_id:02d} {release[1]} | {url}")
                choice_id += 1
        if releases is None:
            inactive_sources.append(source)
            not_found.remove(source)

    if not_found:
        click.echo()
        for source in not_found:
            click.echo(f"No results found from {source}.")

    if inactive_sources:
        for source in inactive_sources:
            click.echo(f"{source} is inactive. Add its credentials to config.toml to enable it.")
    if source_errors:
        click.echo()
        click.secho(f"Failed to scrape {', '.join(source_errors)}.", fg="red")

    return choices


# A store's page for one track or a playlist: not the album's.
_NOT_ALBUM_PAGE = re.compile(r"/(?:track|playlist)/", re.IGNORECASE)
# What a store appends to a title to say the release type: "Title - EP", "Title (Single)".
_TYPE_SUFFIX = re.compile(r"\s*(?:-\s*(?:EP|Single)|[(\[](?:EP|Single)[)\]])\s*$", re.IGNORECASE)


def _metasource_of(url: str) -> str | None:
    """The metadata source that scrapes this URL, if any."""
    return next((name for name, source in METASOURCES.items() if source.Scraper.regex.match(url)), None)


def files_store_url(path: str) -> str | None:
    """The store album URL the files' own tags give under a source key (SOURCE, URL, WWW, ...).

    Only a URL of a store a metadata source scrapes counts, and only when it is the only store album
    URL the tags hold, under any key: two different ones give none.
    """
    sourced, other = tag_urls(path)

    def album(url: str) -> bool:
        return is_store_url(url) and not _NOT_ALBUM_PAGE.search(url) and _metasource_of(url) is not None

    if len({_album_identity(url) for url in sourced + other if album(url)}) != 1:
        return None
    return next((url for url in sourced if album(url)), None)


def _album_identity(url: str) -> tuple[str, ...] | None:
    """(source, release id) of a store album URL, or (source, host, slug) for Bandcamp, so its other forms
    (locale, host, slash) count once."""
    source = _metasource_of(url)
    match = METASOURCES[source].Scraper.regex.search(url) if source else None
    if source is None or match is None:
        return None
    groups = [group.rstrip("/") for group in match.groups() if group]
    # The release id is the last group, kept as written. A Bandcamp slug is unique only on its own host, the
    # first group; Bandcamp writes both in lowercase, so their case does not matter.
    if source == "Bandcamp":
        return (source, groups[0].lower(), groups[-1].lower())
    return (source, groups[-1])


def _comparable_title(title: object) -> str:
    return comparable(_TYPE_SUFFIX.sub("", str(title or "")))


def _matching_choice(
    choices: dict[int, tuple[str, str]], search_results: dict[str, Any], rls_data: dict[str, Any], track_count: int
) -> int | None:
    """The search result whose artist, title, track count and year agree with the files' own tags.

    A count or year a result does not give is not held against it. Two matching results from the same
    source (an explicit and a clean version, say) give none from that source.
    """
    title = _comparable_title(rls_data.get("title"))
    artists = artist_keys(rls_data.get("artists"))
    year = str(rls_data.get("year") or "")[:4]
    if not title or not artists:
        return None
    matches = []
    for choice_id, (source, rls_id) in choices.items():
        ident = ((search_results.get(source) or {}).get(rls_id) or (None,))[0]
        if ident is None or _comparable_title(ident.album) != title or comparable(ident.artist) not in artists:
            continue
        if ident.track_count not in (None, track_count):
            continue
        if year and ident.year and str(ident.year)[:4] != year:
            continue
        matches.append(choice_id)
    for choice_id in matches:
        if sum(choices[other][0] == choices[choice_id][0] for other in matches) == 1:
            return choice_id
    return None


def suggest_choice(
    choices: dict[int, tuple[str, str]],
    search_results: dict[str, Any],
    rls_data: dict[str, Any],
    track_count: int,
    url: str | None,
) -> str | None:
    """The metadata prompt's default: the files' store URL, and the search result that matches them.

    The URL is starred (the release's source) only for a WEB release. The matching result is left
    out when it is from the URL's own store, which already gives that source's metadata.

    Args:
        choices: The numbered search results, as printed.
        search_results: What the search returned, by source and release ID.
        rls_data: The release data built from the tags.
        track_count: The number of audio files.
        url: The files' store URL (files_store_url), or None.

    Returns:
        The answer an empty reply gives, or None for no default.
    """
    parts = [f"{'*' if rls_data.get('source') == 'WEB' else ''}{url}"] if url else []
    match = _matching_choice(choices, search_results, rls_data, track_count)
    if match is not None and (url is None or choices[match][0] != _metasource_of(url)):
        parts.append(str(match))
    return " ".join(parts) or None


def _choice_options(choices: dict[int, tuple[str, str]]) -> interaction.Options:
    """The metadata question's answers, for salmon web (ADR 0006): a card per result under its store's heading, as
    numbered when printed, Manual and Abort, store URLs, and the mark for the store the files came from.

    A result shows its artist, title, year and track count when the search gave them (a _Choice), its number only
    otherwise. The CLI never sees this.
    """
    items = []
    for choice_id, choice in choices.items():
        source, rls_id = choice
        ident = getattr(choice, "ident", None)
        fields: list[tuple[str, str]] = []
        if ident is not None:
            label = f"{ident.artist} - {ident.album}"
            if ident.year:
                fields.append(("Year", str(ident.year)))
            if ident.track_count is not None:
                fields.append(("Tracks", str(ident.track_count)))
        else:
            label = f"Result {choice_id}"
        searcher = SEARCHSOURCES.get(source)
        link = searcher.Searcher.format_url(rls_id) if searcher is not None else None
        card = interaction.Card(tuple(fields), link=link)
        items.append(interaction.Option(str(choice_id), label, group=store_heading(source), card=card))
    items += [interaction.Option("m", "Manual", action=True), interaction.Option("a", "Abort", action=True)]
    return interaction.Options(
        tuple(items),
        several=True,
        free_text=interaction.FreeText("Store URL"),
        mark=interaction.Mark("*", "The store the files came from (WEB)"),
    )


async def _select_choice(
    choices: dict[int, tuple[str, str]], rls_data: dict[str, Any] | None, default: str | None = None
) -> tuple[dict[str, Any], str | None]:
    """Allow the user to select a metadata choice.

    Then, if the metadata came from a scraper, run the scrape(s) and return combined metadata.

    Args:
        choices: Dictionary of choice ID to (source, release_id) tuples.
        rls_data: Release data dictionary.
        default: The answer an empty reply gives (suggest_choice), or None to require one.

    Returns:
        Tuple of (metadata dict, source URL or None).
    """
    source_url = None
    # Initialize rls_data if needed
    rls_data = rls_data or {}
    if "urls" not in rls_data:
        rls_data["urls"] = []

    options = _choice_options(choices)
    while True:
        if choices:
            res = await interaction.prompt(
                click.style(
                    "\nWhich metadata results would you like to use? Type result numbers and/or paste URLs, "
                    'separated by spaces; all of them are combined. Put "*" before the one the files came from '
                    "(the WEB store). Or [m]anual / [a]bort.",
                    fg="magenta",
                ),
                type=click.STRING,
                default=default,
                options=options,
            )
        else:
            res = await interaction.prompt(
                click.style(
                    "\nNo metadata results were found. Paste URLs separated by spaces (all of them are combined), "
                    'with "*" before the one the files came from (the WEB store). Or [m]anual / [a]bort.',
                    fg="magenta",
                ),
                type=click.STRING,
                default=default,
                options=options,
            )

        if res.lower().startswith("m"):
            return await _get_manual_metadata(rls_data), None
        elif res.lower().startswith("a"):
            raise click.Abort

        sources, tasks = [], []
        for r in res.split():
            # Handle starred items first
            stripped = r[1:] if r.startswith("*") else r
            stripped_lower = stripped.lower()

            # Handle URLs (both starred and unstarred)
            if stripped_lower.startswith("http"):
                # Add any URL to rls_data urls if not already there
                if stripped not in rls_data["urls"]:
                    rls_data["urls"].append(stripped)

                # Set source_url if this is a starred URL
                if r.startswith("*"):
                    source_url = stripped

                # Try to scrape if it matches a metadata source
                for name, source in METASOURCES.items():
                    if source.Scraper.regex.match(stripped):
                        sources.append(name)
                        tasks.append(source.Scraper().scrape_release(stripped))
                        break
            # Handle numeric choices
            elif stripped.strip().isdigit() and int(stripped.strip()) in choices:
                scraper = METASOURCES[choices[int(stripped)][0]].Scraper()
                sources.append(choices[int(stripped)][0])
                tasks.append(handle_scrape_errors(scraper.scrape_release_from_id(choices[int(stripped)][1])))
                # Set source_url if this is a starred choice
                if r.startswith("*"):
                    source_url = SEARCHSOURCES[choices[int(stripped)][0]].Searcher.format_url(choices[int(stripped)][1])

        if not tasks:
            # Go to manual mode only if we have any URLs
            if rls_data["urls"]:
                meta = await _get_manual_metadata(rls_data)
                meta["urls"] = meta.get("urls", [])
                # If we have a source_url (from a starred URL), make sure it's included
                if source_url and source_url not in meta["urls"]:
                    meta["urls"].append(source_url)
                return meta, source_url
            continue

        metadatas = await asyncio.gather(*tasks)
        return _combine_scraped(sources, list(metadatas), rls_data, source_url), source_url


async def _get_manual_metadata(rls_data):
    """
    Use the metadata built from the file tags as a base, then allow the user to edit
    that dictionary.
    """
    metadata = json.dumps(rls_data, indent=2, ensure_ascii=False)
    while True:
        try:
            metadata = await interaction.edit(metadata, extension=".json", editor=cfg.upload.default_editor) or metadata
            metadata_dict = msgspec.json.decode(metadata)
            if isinstance(metadata_dict["genres"], str):
                metadata_dict["genres"] = [metadata_dict["genres"]]
            # Typed genres go through the same splitting and whitelist as scraped ones.
            metadata_dict["genres"] = standardize_genres(metadata_dict["genres"])
            return metadata_dict
        except (TypeError, msgspec.DecodeError):
            await interaction.confirm(
                click.style("Metadata is not a valid JSON file, retry?", fg="magenta", bold=True),
                default=True,
                abort=True,
            )


def _print_metadata(metadata, metadata_name="Pending"):
    """Print the metadata that is a part of the new metadata."""
    click.secho(f"\n{metadata_name} metadata:", fg="yellow", bold=True)
    click.echo(f"> TRACK COUNT   : {sum(len(d.values()) for d in metadata['tracks'].values())}")
    click.echo("> ARTISTS:")
    for artist in metadata["artists"]:
        click.echo(f">>>  {artist[0]} [{artist[1]}]")
    click.echo(f"> TITLE         : {metadata['title']}")
    click.echo(f"> GROUP YEAR    : {metadata['group_year']}")
    click.echo(f"> YEAR          : {metadata['year']}")
    click.echo(f"> EDITION TITLE : {metadata['edition_title']}")
    click.echo(f"> LABEL         : {metadata['label']}")
    click.echo(f"> CATNO         : {metadata['catno']}")
    click.echo(f"> UPC           : {metadata['upc']}")
    click.echo(f"> GENRES        : {'; '.join(metadata['genres'])}")
    click.echo(f"> RELEASE TYPE  : {metadata['rls_type']}")
    click.echo(f"> COMMENT       : {metadata['comment']}")
    click.echo("> URLS:")
    for url in metadata["urls"]:
        click.echo(f">>> {url}")


def fix_hardcore_genre(metadata):
    """
    Fix the genre if it contains both rock/metal and dance/electronic, by changing
    "Hardcore" to "Hardcore Rock" or "Hardcore Dance" as appropriate.
    """
    genres = metadata.get("genres", [])

    rock_found = any("rock" in g.lower() or "metal" in g.lower() for g in genres)
    dance_found = any("dance" in g.lower() or "electronic" in g.lower() for g in genres)

    # If both rock and dance are found, don't modify
    if rock_found and dance_found:
        return metadata

    # Determine the replacement text
    replacement = "Hardcore Rock" if rock_found else "Hardcore Dance"

    # Apply replacement if needed
    metadata["genres"] = [replacement if "hardcore" in g.lower() else g for g in genres]

    return metadata


def remove_various_artists(metadata):
    """Drop a "Various Artists" or "Various" credit, which names no artist, from the release's artists and each
    track's. Matched on the whole name, in any case. A track left with no artist goes to the review, which asks
    for one."""

    def named(artists):
        return [(a, i) for a, i in artists if a.strip().casefold() not in {"various artists", "various"}]

    metadata["artists"] = named(metadata["artists"])
    for disc in metadata["tracks"].values():
        for track in disc.values():
            track["artists"] = named(track["artists"])


def clean_metadata(metadata):
    for disc, tracks in metadata["tracks"].items():
        for num, track in tracks.items():
            for artist, importance in copy(track["artists"]):
                guest_artists = {re_strip(a) for a, i in track["artists"] if i in {"guest", "remixer"}}
                if re_strip(artist) in guest_artists and importance == "main":
                    if sum("main" in item for item in metadata["tracks"][disc][num]["artists"]) == 1:
                        pass
                    else:
                        metadata["tracks"][disc][num]["artists"].remove((artist, importance))

    _dedupe_catno_against_upc(metadata)
    return metadata


def _dedupe_catno_against_upc(metadata: dict[str, Any]) -> None:
    """Clear the catalogue number when it is really just the UPC repeated.

    Uses `.get` rather than indexing: manually edited metadata can omit either key entirely.
    """
    catno = metadata.get("catno")
    if catno and catno.replace(" ", "") == str(metadata.get("upc")):
        metadata["catno"] = None
