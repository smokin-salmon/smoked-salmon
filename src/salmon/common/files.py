import os
import stat
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio
import asyncclick as click
from mutagen import MutagenError
from tqdm import tqdm

from salmon import cfg
from salmon.common.constants import AUDIO_EXTENSIONS
from salmon.common.numbers import file_order_key
from salmon.errors import UploadError


def is_torrent_file(name: str) -> bool:
    """Whether a file's name makes it a .torrent file, in any case. By the end of the name: ".torrent" has no suffix."""
    return name.lower().endswith(".torrent")


class AlbumPath(click.Path):
    """A click.Path made absolute without resolving symlinks, so an album symlinked into a library is seen there."""

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        path = os.fsdecode(super().convert(value, param, ctx))
        parts = Path(path).parts
        if os.pardir not in parts:
            return os.path.abspath(path)
        # The filesystem takes "link/.." from where the link leads; abspath would drop both as text.
        last = len(parts) - parts[::-1].index(os.pardir)
        return os.path.join(os.path.realpath(os.path.join(*parts[:last])), *parts[last:])


def shares_files(path: str) -> bool:
    """Whether path is a symlink, or holds a symlink or a file with another hardlink.

    A tag write goes into the shared inode, or through the symlink, so rewriting such a folder in place changes the
    files of another folder too (often a torrent client's, which then fails its recheck). An entry that cannot be read
    counts as sharing: it could hide a link.
    """
    if os.path.islink(path):
        return True
    if not os.path.isdir(path):
        try:
            return os.path.isfile(path) and os.stat(path).st_nlink > 1
        except OSError:  # Gone between the two calls.
            return True
    errors: list[OSError] = []
    for root, folders, files in os.walk(path, onerror=errors.append):
        for name in (*folders, *files):
            try:
                entry = os.lstat(os.path.join(root, name))
            except OSError:  # Gone or unreadable mid-walk.
                return True
            if stat.S_ISLNK(entry.st_mode) or (stat.S_ISREG(entry.st_mode) and entry.st_nlink > 1):
                return True
    return bool(errors)


def rewrite_refusal(path: str) -> str | None:
    """Why salmon must not rewrite or delete the files of path in place, or None if it may.

    Commands that would (compress, sanitize) refuse with this reason; salmon up and salmon tag work on a copy.
    """
    if cfg.directory.is_library_path(path):
        return "it is in library_dirs"
    if (library := cfg.directory.library_inside(path)) is not None:
        return f"it holds the library folder {library}"
    if shares_files(path):
        return "it shares files with another folder through a hardlink or symlink"
    return None


def unreadable_error(error: OSError, path: str) -> UploadError:
    """The error for the album at path, or something in it, that salmon could not read."""
    name = os.path.relpath(error.filename or path, path)
    where = path if name == os.curdir else f"{name} in {path}"
    return UploadError(
        f"Could not read {where} ({error.strerror}): fix it or remove it from the album, then run salmon again."
    )


def album_files(path: str, extensions: Iterable[str], *, skip_unreadable: bool = False) -> list[str]:
    """The files under path whose lower-cased name ends in one of extensions, joined to path, in track order (see
    file_order_key).

    Raises:
        UploadError: Naming a folder that cannot be read, unless skip_unreadable leaves it out. A folder gone
            mid-walk, or a path that is not a folder, lists nothing.
    """

    def stop(error: OSError) -> None:
        if not skip_unreadable and not isinstance(error, (FileNotFoundError, NotADirectoryError)):
            raise unreadable_error(error, path) from error

    suffixes = tuple(extensions)
    found = [
        os.path.join(root, name)
        for root, _folders, names in os.walk(path, onerror=stop)
        for name in names
        if name.lower().endswith(suffixes)
    ]
    return sorted(found, key=file_order_key)


def get_audio_files(path: str, *, skip_unreadable: bool = False) -> list[str]:
    """The audio files under path, named from it, in track order (see file_order_key). See album_files for what it
    raises."""
    return [
        os.path.relpath(file, path) for file in album_files(path, AUDIO_EXTENSIONS, skip_unreadable=skip_unreadable)
    ]


@dataclass
class CompressResult:
    """The outcome of re-compressing a single FLAC file."""

    filepath: str
    success: bool
    error: str | None = None


async def compress(filepath: str) -> CompressResult:
    """Re-compress a .flac file with the configured compression level.

    flac writes to a temp file and only replaces the original once the encode,
    and the verify pass (-V), succeed, so a failure here leaves the original
    file untouched.

    Args:
        filepath: Path to the FLAC file to re-compress.

    Returns:
        Whether the re-encode succeeded, with flac's error message on failure.
    """
    command = [
        "flac",
        f"-{cfg.upload.compression.flac_compression_level}",
        "-V",
        "-s",
        filepath,
        "--force",
    ]
    try:
        result = await anyio.run_process(command, check=False)
    except OSError as e:
        return CompressResult(filepath, False, str(e))
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip() or f"flac exited with code {result.returncode}"
        return CompressResult(filepath, False, error)
    return CompressResult(filepath, True)


async def process_files[T](
    files: list[str],
    process_func: Callable[[str, int], Awaitable[T]],
    desc: str,
    on_done: Callable[[], None] | None = None,
) -> list[T]:
    """Process files concurrently using anyio with a capacity limiter.

    Args:
        on_done: Called each time a file is done, instead of showing a progress bar: nothing is printed.
    """
    results: list[T | None] = [None] * len(files)
    limiter = anyio.CapacityLimiter(cfg.upload.simultaneous_threads)

    async def process_with_result(file: str, idx: int, done: Callable[..., Any]) -> None:
        async with limiter:
            result = await process_func(file, idx)
        results[idx] = result
        done()

    async def process_all(done: Callable[..., Any]) -> None:
        async with anyio.create_task_group() as tg:
            for idx, file in enumerate(files):
                tg.start_soon(process_with_result, file, idx, done)

    if on_done is not None:
        await process_all(on_done)
    else:
        with tqdm(total=len(files), desc=desc, colour="cyan") as pbar:
            await process_all(lambda: pbar.update(1))

    return cast("list[T]", results)


async def process_tracks[T](
    files: list[str],
    process_func: Callable[[str, int], Awaitable[T]],
    desc: str,
    on_done: Callable[[], None] | None = None,
) -> list[T]:
    """process_files for tracks an encoder works on, whose failure is the user's to act on and not a bug.

    Raises:
        UploadError: Naming every track that failed, which is only those failing together: the first cancels the
            others. A full disk or a program not installed is one too. Any other exception stays the task group's.
    """

    async def named(file: str, idx: int) -> T:
        try:
            return await process_func(file, idx)
        except (OSError, MutagenError) as e:
            raise UploadError(f"{os.path.basename(file)}: {e}") from e

    try:
        return await process_files(files, named, desc, on_done)
    except ExceptionGroup as group:
        if not all(isinstance(e, UploadError) for e in group.exceptions):
            raise
        raise UploadError("\n".join(str(e) for e in group.exceptions)) from group
