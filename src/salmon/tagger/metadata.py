import asyncio
import json
import re
from copy import copy
from itertools import islice
from typing import Any

import asyncclick as click
import msgspec

from salmon import cfg
from salmon.common import handle_scrape_errors, make_searchstrs, re_strip
from salmon.search import SEARCHSOURCES, run_metasearch
from salmon.sources.bandcamp import resolve_source_url as resolve_bandcamp_source_url
from salmon.tagger.combine import combine_metadatas
from salmon.tagger.sources import METASOURCES
from salmon.tagger.sources.base import generate_artists


async def get_metadata(
    path: str,
    tags: dict[str, Any],
    rls_data: dict[str, Any],
    preferred_source_url: str | None = None,
) -> tuple[dict[str, Any], str | None]:
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
    if preferred_source_url:
        preferred_source_url = await resolve_bandcamp_source_url(preferred_source_url)
    metadata, source_url = await _select_choice(choices, rls_data, preferred_source_url=preferred_source_url)
    remove_various_artists(metadata["tracks"])
    metadata = fix_hardcore_genre(metadata)
    return metadata, source_url


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
            click.echo(f"{source} is inactive. Update your config.py with the necessary tokens to enable it.")
    if source_errors:
        click.echo()
        click.secho(f"Failed to scrape {', '.join(source_errors)}.", fg="red")

    return choices


async def _select_choice(
    choices: dict[int, tuple[str, str]],
    rls_data: dict[str, Any] | None,
    preferred_source_url: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Allow the user to select a metadata choice.

    Then, if the metadata came from a scraper, run the scrape(s) and return combined metadata.

    Args:
        choices: Dictionary of choice ID to (source, release_id) tuples.
        rls_data: Release data dictionary.

    Returns:
        Tuple of (metadata dict, source URL or None).
    """
    source_url = None
    # Initialize rls_data if needed
    rls_data = rls_data or {}
    if "urls" not in rls_data:
        rls_data["urls"] = []

    while True:
        if preferred_source_url is not None:
            res = f"*{preferred_source_url}"
            preferred_source_url = None
        elif choices:
            res = await click.prompt(
                click.style(
                    "\nWhich metadata results would you like to use? Other "
                    'options: paste URLs, [m]anual, [a], prefix choice or URL with "*" to indicate source (WEB)',
                    fg="magenta",
                ),
                type=click.STRING,
            )
        else:
            res = await click.prompt(
                click.style(
                    "\nNo metadata results were found. Options: paste URLs, "
                    '[m]anual, [a]bort, prefix URL with "*" to indicate source (WEB)',
                    fg="magenta",
                ),
                type=click.STRING,
            )

        if res.lower().startswith("m"):
            return _get_manual_metadata(rls_data), None
        elif res.lower().startswith("a"):
            raise click.Abort

        selected_urls: list[str] = []
        sources, tasks = [], []
        for r in _split_metadata_selection(res):
            # Handle starred items first
            stripped = r[1:] if r.startswith("*") else r
            stripped_lower = stripped.lower()

            # Handle URLs (both starred and unstarred)
            if stripped_lower.startswith("http"):
                _append_unique(selected_urls, stripped)
                _append_unique(rls_data["urls"], stripped)

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
                choice_id = int(stripped.strip())
                source_name, release_id = choices[choice_id]
                scraper = METASOURCES[source_name].Scraper()
                sources.append(source_name)
                tasks.append(handle_scrape_errors(scraper.scrape_release_from_id(release_id)))
                choice_url = SEARCHSOURCES[source_name].Searcher.format_url(release_id)
                _append_unique(selected_urls, choice_url)
                # Set source_url if this is a starred choice
                if r.startswith("*"):
                    source_url = choice_url

        if not tasks:
            # Go to manual mode only if we have any URLs
            if rls_data["urls"]:
                meta = _get_manual_metadata(rls_data)
                meta["urls"] = _merge_urls(selected_urls, meta.get("urls", []))
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
        meta["urls"] = _merge_urls(selected_urls, meta.get("urls", []))
        return meta, source_url


def _split_metadata_selection(response: str) -> list[str]:
    """Split selection input while preserving the user's ordering."""
    return [token for token in re.split(r"[\s,]+", response) if token]


def _append_unique(items: list[str], value: str) -> None:
    """Append a value only once while preserving insertion order."""
    if value not in items:
        items.append(value)


def _merge_urls(selected_urls: list[str], existing_urls: list[str]) -> list[str]:
    """Keep user-selected URLs first, then append any additional discovered URLs."""
    merged: list[str] = []
    for url in selected_urls:
        _append_unique(merged, url)
    for url in existing_urls:
        _append_unique(merged, url)
    return merged


def _get_manual_metadata(rls_data):
    """
    Use the metadata built from the file tags as a base, then allow the user to edit
    that dictionary.
    """
    metadata = json.dumps(rls_data, indent=2, ensure_ascii=False)
    while True:
        try:
            metadata = click.edit(metadata, extension=".json", editor=cfg.upload.default_editor) or metadata
            metadata_dict = msgspec.json.decode(metadata)
            if isinstance(metadata_dict["genres"], str):
                metadata_dict["genres"] = [metadata_dict["genres"]]
            return metadata_dict
        except (TypeError, msgspec.DecodeError):
            click.confirm(
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

    if metadata["catno"] and metadata["catno"].replace(" ", "") == str(metadata["upc"]):
        metadata["catno"] = None
    return metadata
