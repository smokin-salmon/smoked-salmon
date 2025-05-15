import os
import subprocess
import io
import click
import mutagen
import humanfriendly

from PIL import Image
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from mutagen import PaddingInfo
from salmon import config
from salmon.images import get_cover_from_path
from salmon.common import get_audio_files
from salmon.tagger.tagfile import TagFile

STANDARDIZED_TAGS = {
    "date": ["year"],
    "label": ["recordlabel", "organization", "publisher"],
    "catalognumber": ["labelno", "catalog #", "catno"],
}


def check_tags(path):
    """Get and then check the tags for problems. Offer user way to edit tags."""
    click.secho("\nChecking tags...", fg="yellow", bold=True)
    tags = gather_tags(path)
    if not tags:
        raise IndexError("No tracks were found.")

    check_required_tags(tags)

    if config.PROMPT_PUDDLETAG:
        print_a_tag(next(iter(tags.values())))
        if prompt_editor(path):
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
        for t in ["title", "artist", "album", "tracknumber"]:
            missing = []
            if not getattr(tag_item, t, False):
                missing.append(t)
            if missing:
                offending_files.append(f'{fln} ({", ".join(missing)})')

    if offending_files:
        click.secho(
            "The following files do not contain all the required tags: {}.".format(
                ", ".join(offending_files)
            ),
            fg="red",
        )
    else:
        click.secho("Verified that all files contain the required tags.", fg="green")


def print_a_tag(tags):
    """Print all tags in a tag set."""
    for key, value in tags.items():
        click.echo(f"> {key}: {value}")


def prompt_editor(path):
    """Ask user whether or not to open the files in a tag editor."""
    if not click.confirm(
        click.style(
            "\nAre the above tags acceptable? ([n] to open in tag editor)",
            fg="magenta"
        ),
        default=True,
    ):
        with open(os.devnull, "w") as devnull:
            subprocess.call(["puddletag", path], stdout=devnull, stderr=devnull)
        return True
    return False


def standardize_tags(path):
    """
    Change ambiguously defined tags field values into the fields I arbitrarily
    decided are the ones this script will use.
    """
    for filename in get_audio_files(path):
        mut = mutagen.File(os.path.join(path, filename))
        if not mut.tags:
            mut.tags = []
        found_aliased = set()
        for tag, aliases in STANDARDIZED_TAGS.items():
            for alias in aliases:
                if alias in mut.tags:
                    mut.tags[tag] = mut.tags[alias]
                    del mut.tags[alias]
                    found_aliased.add(alias)
        if found_aliased:
            mut.save()
            click.secho(
                f"Unaliased the following tags for {filename}: "
                + ", ".join(found_aliased)
            )


def compress_to_target_size(image, target_size):

    quality = 95

    buffer = io.BytesIO()

    while True:
        image.save(buffer, "jpeg", optimize=True, quality=quality)

        file_size = len(buffer.getvalue())

        if file_size <= target_size:
            print(f"Successfully compressed to {humanfriendly.format_size(file_size, binary=True)}")
            return buffer.getvalue()
        
        quality -= 5

        if quality <= 75:
            print("Quality too low, cannot compress further!")
            break


def get_8kib_padding(info: PaddingInfo):
    return humanfriendly.parse_size("8KiB")


def compress_pictures(path):
    for filename in get_audio_files(path):
        click.secho(f"Processing file: {filename}", fg="blue")
        audio = FLAC(os.path.join(path, filename))

        padding_size = sum(
            block.length for block in audio.metadata_blocks if block.code == 1
        )

        cover_sizes = sum(len(picture.data) for picture in audio.pictures)
        click.secho(
            f"Padding size: {humanfriendly.format_size(padding_size, binary=True)}, Cover size: {humanfriendly.format_size(cover_sizes, binary=True)}",
            fg="cyan",
        )

        cover_file = get_cover_from_path(path)

        if padding_size + cover_sizes > humanfriendly.parse_size("1MiB"):
            click.secho(
                f"Total size ({humanfriendly.format_size(padding_size + cover_sizes, binary=True)}) exceeds 1MiB!",
                fg="yellow",
            )

            for picture in audio.pictures:
                if picture.type == PictureType.COVER_FRONT:
                    if picture.mime == 'image/jpeg':
                        extension = 'jpg'
                    elif picture.mime == 'image/png':
                        extension = 'png'

                    if not cover_file:
                        cover_file = os.path.join(path, f"cover.{extension}")
                        with open(cover_file, "wb") as img:
                            img.write(picture.data)
                        click.secho(f"Extracted cover to: {cover_file}", fg="green")

            audio.clear_pictures()
            audio.save(padding=get_8kib_padding)

        if audio.pictures == []:
            click.secho(f"Attempting to add external cover...", fg="magenta")
            if not cover_file:
                click.secho("No cover file found!", fg="red")
                continue

            with open(cover_file, "rb") as c:
                data = c.read()

            max_embedded_image_size = humanfriendly.parse_size(
                "1MiB"
            ) - humanfriendly.parse_size("8KiB")

            picture = Picture()

            if len(data) < max_embedded_image_size:
                click.secho(
                    f"Cover size ({humanfriendly.format_size(len(data), binary=True)}) within limit",
                    fg="bright_green",
                )
                picture.mime = Image.open(cover_file).get_format_mimetype()
            else:
                click.secho(
                    f"Resizing oversized cover ({humanfriendly.format_size(len(data), binary=True)})...",
                    fg="yellow",
                )
                image = Image.open(cover_file)
                image.thumbnail((1000, 1000))
                data = compress_to_target_size(image, max_embedded_image_size)
                picture.mime = "image/jpeg"

            picture.data = data
            picture.type = PictureType.COVER_FRONT
            audio.add_picture(picture)
            audio.save(padding=get_8kib_padding)
            click.secho(f"Saved {filename} with optimized cover", fg="bright_green")
        else:
            click.secho(f"Existing covers meet size requirements", fg="bright_white")

