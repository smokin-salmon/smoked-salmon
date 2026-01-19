import asyncio
from itertools import chain
from pprint import pprint

import click

from salmon.common import commandgroup
from salmon.constants import (
    ARTIST_IMPORTANCES,
    RELEASE_TYPES,
    SOURCES,
    TAG_ENCODINGS,
)
from salmon.errors import InvalidMetadataError, ScrapeError
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.cover import download_cover_if_nonexistent
from salmon.tagger.foldername import rename_folder
from salmon.tagger.folderstructure import check_folder_structure
from salmon.tagger.metadata import get_metadata
from salmon.tagger.pre_data import construct_rls_data
from salmon.tagger.retagger import rename_files, tag_files
from salmon.tagger.review import review_metadata
from salmon.tagger.sources import run_metadata
from salmon.tagger.tags import check_tags, gather_tags, standardize_tags


def validate_source(ctx, param, value):
    try:
        return SOURCES[value.lower()]
    except KeyError:
        raise click.BadParameter(f"{value} is not a valid source.") from None
    except AttributeError:
        raise click.BadParameter(
            "You must provide a source. Possible sources are: " + ", ".join(SOURCES.values())
        ) from None


def validate_encoding(ctx, param, value):
    try:
        return TAG_ENCODINGS[value.upper()]
    except KeyError:
        raise click.BadParameter(f"{value} is not a valid encoding.") from None
    except AttributeError:
        return None, None


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option(
    "--source",
    "-s",
    type=click.STRING,
    callback=validate_source,
    help=f"Source of files ({'/'.join(SOURCES.values())})",
)
@click.option(
    "--encoding",
    "-e",
    type=click.STRING,
    callback=validate_encoding,
    help="You must specify one of the following encodings if files aren't lossless: " + ", ".join(TAG_ENCODINGS.keys()),
)
@click.option(
    "--overwrite",
    "-ow",
    is_flag=True,
    help="Whether or not to use the original metadata.",
)
@click.option(
    "--auto-rename",
    "-n",
    is_flag=True,
    help="Rename files and folders automatically",
)
def tag(path, source, encoding, overwrite, auto_rename):
    """Interactively tag an album"""
    click.secho(f"\nProcessing {path}", fg="cyan", bold=True)
    standardize_tags(path)
    tags = gather_tags(path)
    audio_info = gather_audio_info(path)
    rls_data = construct_rls_data(tags, audio_info, source, encoding, overwrite=overwrite)

    metadata, _ = get_metadata(path, tags, rls_data)
    metadata = review_metadata(metadata, metadata_validator_base)
    tag_files(path, tags, metadata, auto_rename)

    download_cover_if_nonexistent(path, metadata["cover"])
    tags = check_tags(path)
    path = rename_folder(path, metadata, auto_rename)
    rename_files(path, tags, metadata, auto_rename, None)
    check_folder_structure(path, scene=False)
    click.secho(f"\nProcessed {path}", fg="cyan", bold=True)


@commandgroup.command()
@click.argument("url")
def meta(url):
    """Scrape metadata from release link"""
    try:
        metadata = asyncio.run(run_metadata(url))
        for key in ["encoding", "media", "encoding_vbr", "source"]:
            del metadata[key]
        click.echo()
        pprint(metadata)
    except ScrapeError as e:
        click.secho(f"Scrape failed: {e}", fg="red")


def metadata_validator_base(metadata):
    """Validate that the provided metadata is not an issue."""
    artist_importances = set(i for _, i in metadata["artists"])
    if "main" not in artist_importances:
        raise InvalidMetadataError("You must have at least one main artist.")
    for track in chain.from_iterable([d.values() for d in metadata["tracks"].values()]):
        if "main" not in set(i for _, i in track["artists"]):
            raise InvalidMetadataError("You must have at least one main artist per track.")
    if not all(i in ARTIST_IMPORTANCES for i in artist_importances):
        raise InvalidMetadataError(
            "Invalid artist importance detected: {}.".format(
                ", ".join(i for i in artist_importances.difference(ARTIST_IMPORTANCES.values()))
            )
        )
    try:
        metadata["year"] = int(metadata["year"])
    except (ValueError, TypeError):
        raise InvalidMetadataError("Year is not an integer.") from None
    if metadata["rls_type"] not in RELEASE_TYPES:
        raise InvalidMetadataError("Invalid release type.")
    if not metadata["genres"]:
        raise InvalidMetadataError("You must specify at least one genre.")
    if metadata["source"] == "CD" and metadata["year"] < 1982:
        raise InvalidMetadataError("You cannot have a CD upload from before 1982.")
    if metadata["source"] not in SOURCES.values():
        raise InvalidMetadataError(f"{metadata['source']} is not a valid source.")
    if metadata["label"] and (len(metadata["label"]) < 2 or len(metadata["label"]) > 80):
        raise InvalidMetadataError("Label must be over 2 and under 80 characters.")
    if metadata["label"] is not None and "records dk" in metadata["label"].lower():
        raise InvalidMetadataError(
            "Records DK is not a label. It's a platform for releasing albums. "
            "Please change the label (e.g Self Released)"
        )
    if metadata["catno"] and (len(metadata["catno"]) < 2 or len(metadata["catno"]) > 80):
        raise InvalidMetadataError("Catno must be over 2 and under 80 characters.")

    return metadata
