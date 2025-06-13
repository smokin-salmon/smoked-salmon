import os
import subprocess

import click
import mutagen

from salmon import cfg
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

    if cfg.upload.prompt_puddletag:
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
