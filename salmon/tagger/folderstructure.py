import os
import shutil

import asyncclick as click

from salmon import cfg
from salmon.constants import ALLOWED_EXTENSIONS
from salmon.errors import NoncompliantFolderStructure


async def check_folder_structure(path, scene):
    """
    Run through every filesystem check that causes uploads to violate the rules
    or be rejected on the upload form. Verify that path lengths <180, that there
    are no zero length folders, and that the file extensions are valid.
    """
    while True:
        click.secho("\nChecking folder structure...", fg="cyan", bold=True)
        try:
            await _check_illegal_folders(path)
            _check_path_lengths(path, scene)
            _check_zero_len_folder(path)
            await _check_extensions(path, scene)
            return
        except NoncompliantFolderStructure:
            if scene:
                click.secho(
                    "The folder structure is not compliant with the upload rules. "
                    "As this is a scene release, you need to manually descene it before upload.",
                    fg="red",
                    bold=True,
                )
                raise click.Abort() from None
            click.confirm(
                click.style(
                    "You need to manually fix the issues present in the upload's folder? "
                    "Send a [Y] once you have done so, or a [N] to abort.",
                    fg="magenta",
                    bold=True,
                ),
                default=False,
                abort=True,
            )


async def _check_illegal_folders(path):
    """Verify illegal folders."""
    for root, dirs, _files in os.walk(path, topdown=False):
        for dirname in dirs:
            if dirname == "@eaDir":
                target_dir = os.path.join(root, dirname)
                while True:
                    resp = (
                        await click.prompt(
                            f"Dirname {target_dir} is illegal. [D]elete, [A]bort, or [C]ontinue?",
                            default="D",
                        )
                    ).lower()
                    if resp[0].lower() == "d":
                        shutil.rmtree(target_dir)
                        break
                    elif resp[0].lower() == "a":
                        raise click.Abort
                    elif resp[0].lower() == "c":
                        break


def _check_path_lengths(path, scene):
    """Verify that all path lengths are <=180 characters."""
    offending_files, really_offending_files = [], []
    root_len = len(cfg.directory.download_directory) + 1
    for root, _, files in os.walk(path):
        if len(os.path.abspath(root)) - root_len > 180:
            click.secho("A subfolder has a path length >180 characters.", fg="red")
            raise NoncompliantFolderStructure
        for f in files:
            filepath = os.path.abspath(os.path.join(root, f))
            filepathlen = len(filepath) - root_len
            if filepathlen > 180:
                if filepathlen < 250:
                    offending_files.append(filepath)
                else:
                    really_offending_files.append(filepath)

    if scene and (offending_files or really_offending_files):
        click.secho("The following files exceed 180 characters in length.", fg="red", bold=True)
        for f in offending_files + really_offending_files:
            click.echo(f" >> {f}")
        raise NoncompliantFolderStructure

    if really_offending_files:
        click.secho(
            "The following files exceed 180 characters in length, but cannot "
            "be safely truncated (more than 70 characters above the limit):",
            fg="red",
            bold=True,
        )
        for f in really_offending_files:
            click.echo(f" >> {f}")
        raise NoncompliantFolderStructure

    if not offending_files:
        return click.secho("No paths exceed 180 characters in length.", fg="green")

    click.secho("The following exceed 180 characters in length, truncating...", fg="red")
    for filepath in sorted(offending_files):
        filename, ext = os.path.splitext(filepath)
        newpath = filepath[: 178 - len(filename) - len(ext) * 2 + root_len] + ".." + ext
        os.rename(filepath, newpath)
        click.echo(f" >> {newpath}")


def _check_zero_len_folder(path):
    """Verify that a zero length folder does not exist."""
    for root, _, files in os.walk(path):
        for filename in files:
            foldlist = os.path.join(root, filename)
            if "//" in foldlist:
                click.secho("A zero length folder exists in this directory.", fg="red")
                raise NoncompliantFolderStructure
    click.secho("No zero length folders were found.", fg="green")


async def _check_extensions(path, scene):
    """Validate that all file extensions are valid."""
    mp3, aac, flac = [], [], []
    offending_files = []  # Collect offending files for scene releases
    for root, _, files in os.walk(path):
        for fln in files:
            _, ext = os.path.splitext(fln.lower())
            if ext == ".mp3":
                mp3.append(fln)
            elif ext == ".flac":
                flac.append(fln)
            elif ext == ".m4a":
                aac.append(fln)
            elif ext not in ALLOWED_EXTENSIONS:
                if scene:
                    offending_files.append(os.path.join(root, fln))
                else:
                    await _handle_bad_extension(os.path.join(root, fln), scene)

    if scene and offending_files:
        click.secho("The following files have invalid extensions:", fg="red", bold=True)
        for filepath in offending_files:
            click.echo(f" >> {filepath}")
        raise NoncompliantFolderStructure

    if len([li for li in [mp3, flac, aac] if li]) > 1:
        await _handle_multiple_audio_exts()
    else:
        click.secho("File extensions have been validated.", fg="green")


async def _handle_bad_extension(filepath, scene):
    while True:
        resp = (
            await click.prompt(
                f"{filepath} does not have an approved file extension. [D]elete, [a]bort, or [c]ontinue?",
                default="D",
            )
        ).lower()
        if resp[0].lower() == "d":
            return os.remove(filepath)
        elif resp[0].lower() == "a":
            raise click.Abort
        elif resp[0].lower() == "c":
            return


async def _handle_multiple_audio_exts():
    while True:
        resp = (
            await click.prompt(
                "There are multiple audio codecs in this folder. [A]bort or [c]ontinue?",
                default="A",
            )
        ).lower()
        if resp[0] == "a":
            raise click.Abort
        if resp[0] == "c":
            return
