import os

import anyio
import asyncclick as click
from mutagen import File as MutagenFile
from mutagen import flac, mp3, mp4

from salmon import cfg
from salmon.common import get_audio_files
from salmon.tagger.tagfile import TAG_FIELDS, TagFile

TAG_PRINT_ORDER = [
    "title",
    "artist",
    "album",
    "albumartist",
    "date",
    "genre",
    "composer",
    "conductor",
    "tracknumber",
    "tracktotal",
    "discnumber",
    "disctotal",
    "label",
    "catno",
    "upc",
    "isrc",
    "replay_gain",
    "peak",
    "comment",
]

STANDARDIZED_TAGS = {
    "date": ["year"],
    "label": ["recordlabel", "organization", "publisher"],
    "catalognumber": ["labelno", "catalog #", "catno"],
    "tracktotal": ["totaltracks", "total tracks"],
    "disctotal": ["totaldiscs", "total discs"],
}

CLASSICAL_GENRES = {
    "classical",
    "baroque",
    "chambermusic",
    "choral",
    "modernclassical",
    "orchestral",
    "opera",
}


async def check_tags(path: str) -> dict[str, TagFile]:
    """Get and then check the tags for problems. Offer user way to edit tags.

    Args:
        path: Path to the directory containing audio files.

    Returns:
        Dictionary mapping filenames to their TagFile objects.

    Raises:
        IndexError: If no tracks are found.
    """
    click.secho("\nChecking tags...", fg="yellow", bold=True)
    tags = gather_tags(path)
    if not tags:
        raise IndexError("No tracks were found.")

    check_required_tags(tags)

    if cfg.upload.prompt_puddletag:
        print_a_tag(next(iter(tags.values())))
        if await prompt_editor(path):
            tags = gather_tags(path)

    return tags


def gather_tags(path):
    """Get the tags of each file."""
    tags = {}
    for filename in get_audio_files(path, sort_by_tracknumber=True):
        tags[filename] = TagFile(os.path.join(path, filename))
    return tags


def check_required_tags(tags):
    """Verify that every track has the required tag fields."""
    offending_files = []
    for fln, tag_item in tags.items():
        missing = []
        for t in ["title", "artist", "album", "tracknumber"]:
            if not getattr(tag_item, t, False):
                missing.append(t)
        if _requires_classical_composer(tag_item) and not getattr(tag_item, "composer", False):
            missing.append("composer")
        if missing:
            offending_files.append(f"{fln} ({', '.join(missing)})")

    if offending_files:
        click.secho(
            "The following files do not contain all the required tags: {}.".format(", ".join(offending_files)),
            fg="red",
        )
    else:
        click.secho("Verified that all files contain the required tags.", fg="green")


def _requires_classical_composer(tag_item) -> bool:
    genres = getattr(tag_item, "genre", None)
    if not genres:
        return False
    if isinstance(genres, str):
        genres = [genres]
    return any(str(genre).strip().lower().replace(" ", "") in CLASSICAL_GENRES for genre in genres)


def print_a_tag(tag_item: TagFile) -> None:
    """Print the readable tags of a single file, skipping empty ones.

    TagFile has no `items()`; its `__getattr__` returns None for any unknown
    attribute instead of raising, so we read each of salmon's known field
    names through getattr rather than trying to iterate the object itself.
    """
    mut = tag_item.mut
    if isinstance(mut, flac.FLAC):
        fields = TAG_FIELDS["FLAC"]
    elif isinstance(mut, mp3.MP3):
        fields = TAG_FIELDS["MP3"]
    elif isinstance(mut, mp4.MP4):
        fields = TAG_FIELDS["AAC"]
    else:
        return

    for key in TAG_PRINT_ORDER:
        if key not in fields:
            continue
        value = getattr(tag_item, key, None)
        if not value:
            continue
        if isinstance(value, list):
            value = "; ".join(str(v) for v in value)
        click.echo(f"> {key}: {value}")


async def prompt_editor(path: str) -> bool:
    """Ask user whether or not to open the files in a tag editor.

    Args:
        path: Path to the directory containing audio files.

    Returns:
        True if the editor was opened, False if tags were accepted.
    """
    if not click.confirm(
        click.style("\nAre the above tags acceptable? ([n] to open in tag editor)", fg="magenta"),
        default=True,
    ):
        await anyio.run_process(["puddletag", path], check=False)
        return True
    return False


def standardize_tags(path: str) -> None:
    """Change ambiguously defined tags field values into standardized fields.

    This function renames tag fields to use consistent naming conventions.
    For example, 'year' becomes 'date', 'recordlabel' becomes 'label', etc.

    Args:
        path: Path to the directory containing audio files.
    """
    for filename in get_audio_files(path):
        mut = MutagenFile(os.path.join(path, filename))
        if mut is None:
            continue
        tags = mut.tags
        if tags is None:
            continue
        found_aliased: set[str] = set()
        for tag, aliases in STANDARDIZED_TAGS.items():
            for alias in aliases:
                if alias in tags:
                    # Mutagen tags support dynamic key access for Vorbis comments
                    tags[tag] = tags[alias]
                    del tags[alias]
                    found_aliased.add(alias)
        if found_aliased:
            mut.save()
            click.secho(f"Unaliased the following tags for {filename}: " + ", ".join(found_aliased))
