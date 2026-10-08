import asyncio
import json
import os
from copy import copy
from itertools import islice
from typing import Any

import asyncclick as click
import msgspec

from salmon import cfg
from salmon.common import handle_scrape_errors, make_searchstrs, re_strip
from salmon.search import SEARCHSOURCES, run_metasearch
from salmon.tagger.combine import combine_metadatas
from salmon.tagger.info_txt import find_info_txt, parse_info_txt
from salmon.tagger.sources import METASOURCES
from salmon.tagger.sources.base import generate_artists


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
    info_metadata = await _maybe_load_info_txt(path, rls_data)
    searchstrs = make_searchstrs(rls_data["artists"], rls_data["title"])
    click.secho(f"Searching for '{searchstrs}' releases...")
    artists_list = [a for a, _ in rls_data["artists"]]
    album_title = rls_data["title"]
    search_results = await run_metasearch(
        searchstrs,
        filter=False,
        track_count=len(tags),
        artists=artists_list,
        album=album_title,
        sources={"Discogs": SEARCHSOURCES["Discogs"]} if info_metadata is not None else None,
    )
    choices = _print_search_results(search_results, rls_data)
    metadata, source_url = await _select_choice(choices, rls_data, info_metadata)
    remove_various_artists(metadata["tracks"])
    metadata = fix_hardcore_genre(metadata)
    return metadata, source_url


async def _maybe_load_info_txt(path: str, rls_data: dict[str, Any]) -> dict[str, Any] | None:
    """Offer a neighbouring info.txt as preferred metadata; delete it once parsed."""
    info_path = find_info_txt(path)
    if not info_path:
        return None
    if not await click.confirm("Found info.txt - use it?", default=True):
        return None
    try:
        metadata = parse_info_txt(info_path)
    except Exception as e:
        click.secho(f"Failed to parse info.txt: {e}", fg="red")
        return None
    os.remove(info_path)
    for key in ("source", "scene", "encoding", "format"):
        if metadata.get(key) is None:
            metadata[key] = rls_data.get(key)
    metadata["urls"] = rls_data.get("urls", [])
    return metadata


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
    info_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Allow the user to select a metadata choice.

    Then, if the metadata came from a scraper, run the scrape(s) and return combined metadata.
    When info.txt metadata is given, pasted non-Discogs URLs are kept as description
    links only, and the InfoTxt metadata is prepended as the preferred source.

    Args:
        choices: Dictionary of choice ID to (source, release_id) tuples.
        rls_data: Release data dictionary.
        info_metadata: Parsed info.txt metadata, or None for the normal flow.

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
            if info_metadata is not None:
                info_metadata["urls"] = rls_data.get("urls", [])
                return _get_manual_metadata(info_metadata), None
            return _get_manual_metadata(rls_data), None
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
                    if info_metadata is not None and name != "Discogs":
                        continue
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
                if info_metadata is not None:
                    info_metadata["urls"] = rls_data["urls"]
                    meta = _get_manual_metadata(info_metadata)
                else:
                    meta = _get_manual_metadata(rls_data)
                meta["urls"] = meta.get("urls", [])
                # If we have a source_url (from a starred URL), make sure it's included
                if source_url and source_url not in meta["urls"]:
                    meta["urls"].append(source_url)
                return meta, source_url
            continue

        metadatas = await asyncio.gather(*tasks)
        pairs = [(s, m) for s, m in zip(sources, metadatas, strict=False) if m]
        if info_metadata is not None:
            pairs.insert(0, ("InfoTxt", info_metadata))
        meta = combine_metadatas(*pairs, base=rls_data, source_url=source_url)
        meta = clean_metadata(meta)
        meta["artists"], meta["tracks"] = generate_artists(meta["tracks"])
        return meta, source_url


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
