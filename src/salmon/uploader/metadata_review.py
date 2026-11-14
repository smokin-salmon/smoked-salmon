"""The review of an upload's metadata, its retag and rename, and the dupe checks that follow it."""

import functools
import os
import shutil
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import asyncclick as click

from salmon import cfg, interaction
from salmon.checks.integrity import resolve_integrity_for_upload
from salmon.checks.tag_rules import process_tag_issues
from salmon.common import tagify
from salmon.constants import ENCODINGS, FORMATS
from salmon.converter.conversions import carry_conversion
from salmon.errors import InvalidMetadataError, RequestError, UploadError
from salmon.tagger import metadata_validator_base
from salmon.tagger.ai_review import review_metadata_with_ai
from salmon.tagger.audio_info import gather_audio_info, recompress_path
from salmon.tagger.foldername import rename_folder
from salmon.tagger.folderstructure import check_folder_structure
from salmon.tagger.retagger import collect_album_data, rename_files, tag_files
from salmon.tagger.review import review_metadata
from salmon.tagger.tags import check_tags, gather_tags
from salmon.uploader.dupe_checker import (
    NO_DUPE_SEARCH,
    can_check_site_log,
    check_existing_group,
    dupe_check_recent_torrents,
    generate_dupe_check_searchstrs,
    print_recent_upload_results,
)

if TYPE_CHECKING:
    from salmon.tagger.tagfile import TagFile


async def edit_metadata(
    path: str,
    tags: dict[str, "TagFile"],
    metadata: dict[str, Any],
    source_url: str | None,
    source: str,
    rls_data: dict[str, Any],
    recompress: bool,
    auto_rename: bool,
    spectral_ids: dict[int, str] | None,
    skip_integrity_check: bool = False,
    essential_only: bool = False,
    skip_initial_review: bool = False,
    apply_ai_suggestions: bool = False,
    rename_into: str | None = None,
    original: str | None = None,
    max_path_length: int = 180,
    rls_type_hint: str | None = None,
    offer_other_results: bool = False,
) -> tuple[str, dict[str, Any], dict[str, "TagFile"], dict[str, dict[str, Any]]]:
    """Edit release metadata in an interactive loop until the user confirms.

    Repeatedly prompts the user to review and edit metadata, then applies tags,
    renames files and folder, checks integrity, and confirms readiness for upload.

    Args:
        path: Path to the release directory.
        tags: Mapping of filename to TagFile objects.
        metadata: Release metadata dictionary.
        source: Source string (e.g. "WEB", "CD").
        rls_data: Release data dictionary from pre_data construction.
        recompress: Whether to recompress audio files after tagging.
        auto_rename: Whether to automatically rename files and folder.
        spectral_ids: Mapping of track index to spectral image ID, or None.
        skip_integrity_check: Whether to skip the integrity check step.
        essential_only: If True, only essential extensions are allowed.
        skip_initial_review: Skip the first manual metadata review before AI review.
        apply_ai_suggestions: Automatically apply AI review suggestions when present.
        rename_into: The directory the renamed folder goes into, instead of download_directory.
        original: The folder the user gave, when path is a copy of it, for the folder rename's remove_source_dir note.
        max_path_length: The longest in-torrent path the folder structure check allows.
        rls_type_hint: The release type prompt's default, when the release type has to be asked.
        offer_other_results: Offer [b]ack to the metadata results at the reviews before the files are first written.

    Returns:
        A tuple of (path, metadata, tags, audio_info) after editing is complete.

    Raises:
        click.Abort: If a scene release fails the integrity check, or a file does not decode.
        PickOtherResults: If the user went back to the metadata results, before anything was written.
    """
    given = path
    # The metadata as the pick gave it: going back drops every change since, the AI review's included.
    picked = deepcopy(metadata) if offer_other_results else None
    written: dict[str, Any] = {}  # The album fields the last pass that retagged the files wrote, with their values
    while True:
        metadata = await review_metadata_with_ai(
            metadata,
            rls_data,
            source_url,
            metadata_validator,
            functools.partial(review_metadata, rls_type_hint=rls_type_hint, picked=picked),
            skip_initial_review=skip_initial_review,
            apply_suggestions=apply_ai_suggestions,
        )
        tracks_by_file = None
        if not metadata["scene"]:
            # The first pass removes nothing: only what an earlier pass wrote can go.
            clear = {}
            if written:
                album = collect_album_data(metadata)
                clear = {tag: value for tag, value in written.items() if tag not in album}
            tracks_by_file = await tag_files(path, tags, metadata, auto_rename, clear=clear)
            if tracks_by_file is not None:
                # A field no file has a place for (a catalogue number in MP3s) was not written, so is never cleared.
                album = collect_album_data(metadata)
                written = {tag: value for tag, value in album.items() if any(t.has_field(tag) for t in tags.values())}

        tags = await check_tags(path)
        tag_messages = process_tag_issues(
            path,
            gather_audio_info(path),
            scene=metadata["scene"],
            recompress=recompress and not metadata["scene"],
        )
        if tag_messages:
            click.secho("\nTag notes:", fg="yellow", bold=True)
            for message in tag_messages:
                click.secho(f"  - {message}", fg="yellow")
        if not metadata["scene"] and recompress:
            try:
                await recompress_path(path)
            except UploadError as e:
                raise UploadError(f"{e} Rerun without -c.") from e
        renamed = await rename_folder(path, metadata, auto_rename, parent=rename_into, original=original, source=given)
        if renamed != path:
            original = None  # A re-run renames the renamed folder, which is no longer the copy of the original.
            if (
                path != given
                and cfg.upload.formatting.remove_source_dir
                and os.path.isdir(path)
                and not os.path.samefile(path, renamed)
                and not cfg.directory.protects(path)
            ):
                # Made by an earlier pass of this loop, not the user's: the source itself is removed when the run ends.
                shutil.rmtree(path)
                carry_conversion(path, renamed)
        path = renamed
        if not metadata["scene"]:
            await rename_files(path, tags, metadata, auto_rename, spectral_ids, source, tracks_by_file=tracks_by_file)
        await check_folder_structure(
            path,
            metadata["scene"],
            essential_only=essential_only,
            max_path_length=max_path_length,
            # Neither a copy of the album (original) nor a rename out of it: the folder the user gave.
            album_itself=original is None and os.path.samefile(path, given),
        )

        if not skip_integrity_check:
            await resolve_integrity_for_upload(
                path, scene=metadata["scene"], assume_yes=await interaction.assume_defaults()
            )

        if await interaction.assume_defaults() or await interaction.confirm(
            click.style("\nWould you like to upload the torrent? (No to re-run metadata section)", fg="magenta"),
            default=True,
            labels=("Upload", "Re-run the metadata step"),
        ):
            metadata["tags"] = convert_genres(metadata["genres"], metadata.get("group_year"))
            break

        # Refresh tags to accomodate differences in file structure.
        tags = gather_tags(path)
        # The files now carry this pick: going back would leave its album tags in them.
        picked = None

    tags = gather_tags(path)
    audio_info = gather_audio_info(path)
    return path, metadata, tags, audio_info


async def recheck_dupe(gazelle_site, searchstrs, metadata):
    """Rechecks for a dupe if the artist, album or catno have changed.

    With no search string before the review or after it, asks for a group with nothing searched; -yyy stops instead.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Original search strings.
        metadata: Release metadata.

    Returns:
        Group ID if found, None otherwise.
    """
    new_searchstrs = generate_dupe_check_searchstrs(metadata["artists"], metadata["title"], metadata["catno"])
    if not searchstrs and not new_searchstrs:
        # No search string before the review or after it (#931): the group question has not been asked yet.
        if await interaction.assume_defaults():
            click.secho(
                f"\n{NO_DUPE_SEARCH} Not uploading with -yyy (upload.yes_all): give --group-id, or run without -yyy, "
                "and with upload.yes_all off.",
                fg="red",
                bold=True,
            )
            raise click.exceptions.Exit(1)
        return await check_existing_group(gazelle_site, [], our_title=metadata["title"], release=metadata)
    if searchstrs and any(n not in searchstrs for n in new_searchstrs) or not searchstrs and new_searchstrs:
        click.secho(
            f"\nRechecking for dupes on {gazelle_site.site_string} due to metadata changes...",
            fg="cyan",
            bold=True,
            nl=False,
        )
        return await check_existing_group(gazelle_site, new_searchstrs, our_title=metadata["title"], release=metadata)
    return None


async def last_min_dupe_check(gazelle_site, searchstrs, our_title=None):
    """Check for dupes in the log one last time before upload.

    Helpful if you are uploading something in race like conditions.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings for dupe checking.
        our_title: Our release's title, passed through to dupe_check_recent_torrents.
    """
    if not searchstrs or not can_check_site_log(gazelle_site):
        return
    # Should really avoid asking if already shown the same releases from the log.
    click.secho(f"Last Minute Dupe Check on {gazelle_site.site_code}", fg="cyan")
    try:
        recent_uploads = await dupe_check_recent_torrents(gazelle_site, searchstrs, our_title)
    except RequestError as e:
        click.secho(f"Could not read the {gazelle_site.site_string} site log: {e}", fg="red", bold=True)
        # -yyy takes the question's default, no: the confirm below would wait for an answer.
        if await interaction.assume_defaults():
            raise UploadError(
                f"Not uploading to {gazelle_site.site_string} without the last-minute dupe check with -yyy "
                "(upload.yes_all): run again once its site log can be read, or without -yyy to choose."
            ) from None
        if not await interaction.confirm(
            click.style("\nUpload without the last-minute dupe check?", fg="red", bold=True),
            default=False,
        ):
            raise click.Abort from None
        return
    if recent_uploads:
        print_recent_upload_results(gazelle_site, recent_uploads, " / ".join(searchstrs))
        if not await interaction.confirm(
            click.style(
                "\nWould you still like to upload?",
                fg="red",
                bold=True,
            ),
            default=False,
        ):
            raise click.Abort
    else:
        click.secho(f"Nothing found on {gazelle_site.site_code}", fg="green")


def metadata_validator(metadata):
    """Validate that the provided metadata is not an issue."""
    metadata = metadata_validator_base(metadata)
    if metadata["format"] not in FORMATS.values():
        raise InvalidMetadataError(f"{metadata['format']} is not a valid format.")
    if metadata["encoding"] not in ENCODINGS:
        raise InvalidMetadataError(f"{metadata['encoding']} is not a valid encoding.")

    return metadata


# OPS refuses a new group's tags past this many bytes (upload_handle.php).
_MAX_TAGS_BYTES = 200


def convert_genres(genres, year: str | int | None = None):
    """Convert the weirdly spaced genres to RED-compliant genres, and add year's decade if add_decade_tag asks it."""
    tags = [t for t in (tagify(g) for g in genres) if t]
    decade = _decade_tag(year) if cfg.upload.add_decade_tag else None
    if not decade or decade in tags:
        return ",".join(tags)
    if len(",".join([*tags, decade]).encode()) > _MAX_TAGS_BYTES:
        click.secho(
            f"Not adding the {decade} tag: the tags would pass the {_MAX_TAGS_BYTES} bytes OPS takes.", fg="yellow"
        )
        return ",".join(tags)
    return ",".join([*tags, decade])


def _decade_tag(year: str | int | None) -> str | None:
    """The decade of a four-digit year ("1990s"), or None for any other value."""
    if year is None:
        return None
    try:
        number = int(year)
    except (TypeError, ValueError):
        return None
    return f"{number // 10 * 10}s" if 1000 <= number <= 9999 else None
