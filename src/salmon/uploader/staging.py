"""Copies of an album to work on, so the folder an upload starts from is never modified."""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

import asyncclick as click

from salmon import cfg, dryrun
from salmon.converter.conversions import carry_conversion
from salmon.errors import UploadError

# Scratch copies live here, one directory per run, removed when the run ends.
STAGING_DIR = ".salmon-staging"


@contextmanager
def staged_source(path: str, scratch: bool) -> Iterator[tuple[str, str | None]]:
    """Give the folder an upload works on, and the directory its folder rename stays in.

    An album inside a library_dirs entry is always worked on as a copy in a run directory of its own, like a
    scratch copy, but its rename goes into download_directory, where the renamed copy stays to be seeded.

    Args:
        path: The album folder the upload starts from.
        scratch: Work on a copy in a directory of this run's own under download_directory/.salmon-staging,
            removed when the run ends (success, abort or error). Otherwise the upload works on path itself.

    Yields:
        The folder to work on, and the directory its rename stays in (None for download_directory).

    Raises:
        UploadError: If path holds a library_dirs entry, or the copy does not fit on the disk or fails.
    """
    in_library = cfg.directory.is_library_path(path)
    if not in_library and (library := cfg.directory.library_inside(path)) is not None:
        raise UploadError(f"{path} holds the library folder {library}: run salmon on one album folder of it.")
    if not scratch and not in_library:
        yield path, None
        return
    scratch_dir = _new_scratch_dir()
    try:
        # A scratch copy (--skip-flac-upload, --dry-run) and every folder made from it go when the run ends.
        yield _copy_into(path, scratch_dir, scratch), (scratch_dir if scratch else None)
    finally:
        _remove_scratch_dir(scratch_dir, path)


def _new_scratch_dir() -> str:
    """Make a directory of this run's own under download_directory/.salmon-staging; other runs' stay put."""
    root = os.path.join(cfg.directory.download_directory, STAGING_DIR)
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(dir=root, prefix="run-")


def _copy_into(path: str, into: str, scratch: bool) -> str:
    """Copy the album folder into `into` and return the copy's path."""
    dest = os.path.join(into, os.path.basename(path.rstrip(os.sep)))
    if not scratch:
        why = "It is in library_dirs, so salmon works on a copy and never modifies the library album."
    elif dryrun.active():
        why = "A dry run works on a copy, so the source is never modified."
    else:
        why = "--skip-flac-upload works on a copy, so the source is never modified."
    size = sum(os.path.getsize(os.path.join(root, f)) for root, _, files in os.walk(path) for f in files)
    free = shutil.disk_usage(into).free
    if size > free:
        raise UploadError(
            f"Cannot copy {path} ({size / 1e6:.0f} MB) to {into}: only {free / 1e6:.0f} MB free there. {why}"
        )
    click.secho(f"\nCopying {path} ({size / 1e6:.0f} MB) to {dest}. {why}", fg="cyan")
    if not scratch:
        click.secho(f"The renamed copy goes into {cfg.directory.download_directory} and stays there.", fg="cyan")
    try:
        # A real copy: a hardlink shares the inode, so a later tag write would reach the source.
        shutil.copytree(path, dest)
    except OSError as error:
        raise UploadError(f"Could not copy {path} to {dest}: {error}") from error
    # The record lives beside the album, not in it, so the copy would otherwise leave it behind.
    carry_conversion(path, dest)
    return dest


def _remove_scratch_dir(scratch_dir: str, source: str) -> None:
    """Remove a directory made by _new_scratch_dir, and nothing that resolves anywhere else."""
    root = os.path.realpath(os.path.join(cfg.directory.download_directory, STAGING_DIR))
    real = os.path.realpath(scratch_dir)
    real_source = os.path.realpath(source)
    # Checked on the resolved paths: a symlinked component must not carry the removal out of the root,
    # and neither the source nor a folder holding it is ever removed.
    if os.path.islink(scratch_dir) or os.path.dirname(real) != root or _holds(real, real_source):
        click.secho(f"Left the scratch copy at {scratch_dir}: it is not a run directory in {root}.", fg="yellow")
        return
    try:
        shutil.rmtree(real)
    except OSError as error:
        click.secho(f"Could not remove the scratch copy at {scratch_dir}: {error}", fg="yellow")


def _holds(folder: str, path: str) -> bool:
    """Whether path is folder or inside it."""
    try:
        return os.path.commonpath([folder, path]) == folder
    except ValueError:  # Different drives on Windows.
        return False
