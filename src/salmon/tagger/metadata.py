import asyncio
import json
import re
from copy import copy
from itertools import islice
from typing import Any

import asyncclick as click
import msgspec

from salmon import cfg, interaction
from salmon.checks.source import is_store_url
from salmon.common import handle_scrape_errors, make_searchstrs, re_strip
from salmon.common.strings import artist_keys, comparable
from salmon.search import SEARCHSOURCES, run_metasearch
from salmon.sources.deezer import DeezerBase, album_upc
from salmon.tagger.combine import combine_metadatas
from salmon.tagger.sources import METASOURCES
from salmon.tagger.sources.base import generate_artists, standardize_genres
from salmon.tagger.tag_urls import tag_urls


async def get_metadata(path: str, tags: dict[str, Any], rls_data: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Get metadata pertaining to a release from various metadata sources.

    Have the user decide which sources to use, and then combine their information.

    Args:
        path: Path to the album folder.
        tags: Tag data from audio files.
        rls_data: Release data dictionary.

    Returns:
        Tuple of (metadata dict, source URL or None).
    """
    click.secho("\nChecking metadata...", fg="cyan", bold=True)
    if not rls_data:
        raise ValueError("rls_data cannot be None")
    searchstrs = make_searchstrs(rls_data["artists"], rls_data["title"])
    click.secho(f"Searching for '{searchstrs}' releases...")
    artists_list = [a for a, _ in rls_data["artists"]]
    album_title = rls_data["title"]
    search_results = await run_metasearch(
        searchstrs, filter=False, track_count=len(tags), artists=artists_list, album=album_title
    )
    choices = _print_search_results(search_results, rls_data)
    default = suggest_choice(choices, search_results, rls_data, len(tags), files_store_url(path))
    metadata, source_url = await _select_choice(choices, rls_data, default=default)
    await fill_upc_from_deezer(metadata, path)
    _dedupe_catno_against_upc(metadata)
    remove_various_artists(metadata["tracks"])
    metadata = fix_hardcore_genre(metadata)
    return metadata, source_url


def _first_deezer_album_url(urls: list[str]) -> str | None:
    for url in urls:
        match = DeezerBase.regex.search(url)
        if match and match[1] == "album":
            return url
    return None


async def fill_upc_from_deezer(metadata: dict[str, Any], path: str) -> None:
    """Take a missing UPC from the files' own Deezer album; another store's release may carry a different barcode.

    Makes one request, and only when the UPC is still empty and the files carry a Deezer album URL.
    """
    if metadata.get("upc"):
        return
    sourced, other = tag_urls(path)
    url = _first_deezer_album_url(sourced) or _first_deezer_album_url(other)
    if not url:
        return
    metadata["upc"] = await album_upc(url)


def _print_search_results(results, rls_data=None):
    """Print the results from the metadata source."""
    if rls_data:
        _print_metadata(rls_data, metadata_name="Previous")

    choices = {}
    choice_id = 1
    not_found = list(SEARCHSOURCES.keys())
    inactive_sources = []
    source_errors = SEARCHSOURCES.keys() - [r for r in results]

    for source, releases in results.items():
        if releases:
            click.secho(f"\nResults for {source}:", fg="yellow", bold=True)
            not_found.remove(source)
            results = dict(islice(releases.items(), cfg.upload.search.limit))
            for rls_id, release in results.items():
                choices[choice_id] = (source, rls_id)
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

    if len({url.rstrip("/") for url in sourced + other if album(url)}) != 1:
        return None
    return next((url for url in sourced if album(url)), None)


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

    while True:
        if choices:
            res = await interaction.prompt(
                click.style(
                    "\nWhich metadata results would you like to use? Other "
                    'options: paste URLs, [m]anual, [a], prefix choice or URL with "*" to indicate source (WEB)',
                    fg="magenta",
                ),
                type=click.STRING,
                default=default,
            )
        else:
            res = await interaction.prompt(
                click.style(
                    "\nNo metadata results were found. Options: paste URLs, "
                    '[m]anual, [a]bort, prefix URL with "*" to indicate source (WEB)',
                    fg="magenta",
                ),
                type=click.STRING,
                default=default,
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
        meta = combine_metadatas(
            *((s, m) for s, m in zip(sources, metadatas, strict=False) if m), base=rls_data, source_url=source_url
        )
        meta = clean_metadata(meta)
        meta["artists"], meta["tracks"] = generate_artists(meta["tracks"])
        return meta, source_url


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


def remove_various_artists(tracks):
    for _dnum, disc in tracks.items():
        for _tnum, track in disc.items():
            artists = []
            for artist, importance in track["artists"]:
                if "various artists" not in artist.lower() or artist.lower().strip() != "various":
                    artists.append((artist, importance))
            track["artists"] = artists


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
