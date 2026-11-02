import os
import re
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

import anyio
import asyncclick as click
from tqdm import tqdm

from salmon import cfg

T = TypeVar("T")


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


def get_audio_files(path, sort_by_tracknumber=False):
    """
    Iterate over a path and return all the files that match the allowed
    audio file extensions.
    """
    files = []
    for root, _folders, files_ in os.walk(path):
        files += [
            create_relative_path(root, path, f)
            for f in files_
            if os.path.splitext(f.lower())[1] in {".flac", ".mp3", ".m4a"}
        ]
    if sort_by_tracknumber:
        return sorted(files, key=_tracknumber_sort_key)
    return sorted(files)


def _tracknumber_sort_key(filename):
    """
    Extract a sort key for the filename. Filenames with numbers are sorted
    numerically by the first number found. Filenames without numbers are
    sorted lexicographically.
    """
    match = re.search(r"^(\d+)", filename)
    if match:
        # Return a tuple: (0, track number as integer)
        return (0, int(match.group(1)))
    else:
        # Return a tuple: (1, filename as-is for lexicographical sorting)
        return (1, filename.lower())


def create_relative_path(root, path, filename):
    """
    Create a relative path to a filename. For example, given:
        root     = '/home/xxx/Tidal/Album/Disc 1'
        path     = '/home/xxx/Tidal/Album'
        filename = '01. Track.flac'
    'Disc 1/01. Track.flac' would be returned.
    """
    return os.path.join(root.split(path, 1)[1][1:], filename)  # [1:] to get rid of the slash.


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


async def process_files(
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
