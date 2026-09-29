import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar, cast

import anyio
from tqdm import tqdm

from salmon import cfg

T = TypeVar("T")


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
        filepath,
        "--force",
    ]
    try:
        result = await anyio.run_process(command, check=False)
    except FileNotFoundError as e:
        return CompressResult(filepath, False, str(e))
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip() or f"flac exited with code {result.returncode}"
        return CompressResult(filepath, False, error)
    return CompressResult(filepath, True)


async def process_files(
    files: list[str],
    process_func: Callable[[str, int], Awaitable[T]],
    desc: str,
) -> list[T]:
    """Process files concurrently using anyio with a capacity limiter."""
    results: list[T | None] = [None] * len(files)
    limiter = anyio.CapacityLimiter(cfg.upload.simultaneous_threads)

    with tqdm(total=len(files), desc=desc, colour="cyan") as pbar:

        async def process_with_result(file: str, idx: int) -> None:
            async with limiter:
                result = await process_func(file, idx)
            results[idx] = result
            pbar.update(1)

        async with anyio.create_task_group() as tg:
            for idx, file in enumerate(files):
                tg.start_soon(process_with_result, file, idx)

    return cast("list[T]", results)
