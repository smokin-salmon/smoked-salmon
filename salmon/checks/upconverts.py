import math
import os
import re

import anyio
import asyncclick as click
import msgspec
from mutagen import MutagenError, flac

from salmon.common.files import process_files
from salmon.errors import UpconvertCheckError


class UpconvertCheckResult(msgspec.Struct, frozen=True):
    """Result of an upconvert check for a single file."""

    filepath: str
    is_upconverted: bool
    wasted_bits: int
    bitdepth: int


async def upload_upconvert_test(path: str) -> None:
    """Test for upconverted audio files and prompt user to abort if detected.

    Args:
        path: Path to a file or directory to check.

    Raises:
        click.Abort: If upconverts are detected and user confirms abort.
    """
    any_upconverts = await test_upconverted(path)
    if any_upconverts:
        if click.confirm(
            click.style(
                "Possible upconverts detected. Would you like to quit uploading?",
                fg="red",
            ),
            default=True,
        ):
            raise click.Abort
    else:
        click.secho(
            click.style(
                "No upconverts detected (This is not a 100 percent accurate process).",
                fg="green",
            ),
        )


async def test_upconverted(path: str) -> bool | None:
    """Check whether audio files at the given path are upconverted.

    Args:
        path: Path to a single FLAC file or a directory of FLAC files.

    Returns:
        True if any upconverts detected, False if none, None if not applicable.
    """
    if os.path.isfile(path):
        result = await _upconvert_check_handler(path)
        if result is None:
            return None
        _display_results([result])
        return result.is_upconverted
    elif os.path.isdir(path):
        flac_files = []
        for root, _, files in os.walk(path):
            for f in files:
                if f.lower().endswith(".flac"):
                    flac_files.append(os.path.join(root, f))
        results = await process_files(
            flac_files,
            _upconvert_check_handler,
            "Checking FLAC files for upconverts",
        )
        valid_results = [r for r in results if r is not None]
        _display_results(valid_results)
        return any(r.is_upconverted for r in valid_results) if valid_results else None
    return None


async def _upconvert_check_handler(filepath: str, _: int | None = None) -> UpconvertCheckResult | None:
    """Handle upconvert check for a single file, logging errors.

    Args:
        filepath: Path to the FLAC file to check.
        _: Unused index parameter for process_files compatibility.

    Returns:
        UpconvertCheckResult on success, None if the file cannot be analyzed.
    """
    try:
        return await check_upconvert(filepath)
    except UpconvertCheckError as e:
        click.secho(f"{os.path.basename(filepath)}: {e}", fg="yellow")
        return None


async def check_upconvert(filepath: str) -> UpconvertCheckResult:
    """Analyze a FLAC file's wasted bits to detect potential upconversion.

    Args:
        filepath: Path to the FLAC file to analyze.

    Returns:
        UpconvertCheckResult with analysis results.

    Raises:
        UpconvertCheckError: If the file cannot be analyzed.
    """
    try:
        mut = flac.FLAC(filepath)
        bitdepth = mut.info.bits_per_sample
    except MutagenError as e:
        raise UpconvertCheckError("This is not a FLAC file.") from e

    if bitdepth == 16:
        raise UpconvertCheckError("This is a 16bit FLAC file.")

    try:
        response = await anyio.run_process(["flac", "-ac", filepath], check=False)
    except FileNotFoundError as e:
        raise UpconvertCheckError(f"flac binary not found: {e}") from e
    response_text = response.stdout.decode() if response.stdout else ""

    wasted_bits_list: list[int] = []
    for line in response_text.split("\n"):
        r = re.search(r"wasted_bits=(\d+)", line)
        if r:
            wasted_bits_list.append(int(r[1]))

    if not wasted_bits_list:
        raise UpconvertCheckError("Could not determine wasted bits.")

    wasted_bits = math.ceil(sum(wasted_bits_list) / len(wasted_bits_list))
    return UpconvertCheckResult(
        filepath=filepath,
        is_upconverted=wasted_bits >= 8,
        wasted_bits=wasted_bits,
        bitdepth=bitdepth,
    )


def _tracknumber_sort_key(file_path):
    """
    Extract a sort key from the filename. If the filename starts with a number,
    sort numerically by that number. Otherwise, sort lexicographically.
    """
    filename = os.path.basename(file_path)  # Extract just the filename
    match = re.match(r"^(\d+)", filename)  # Match leading numbers

    if match:
        return (0, int(match.group(1)))  # Numeric sort priority
    else:
        return (1, filename.lower())  # Lexicographic sort for non-numbered files


def _display_results(results: list[UpconvertCheckResult]) -> None:
    """Display upconvert check results sorted by track number.

    Args:
        results: List of UpconvertCheckResult objects.
    """
    sorted_results = sorted(results, key=lambda x: _tracknumber_sort_key(x.filepath))

    for result in sorted_results:
        status = "likely upconverted" if result.is_upconverted else "does not have a high number of wasted bits"
        color = "red" if result.is_upconverted else "green"
        click.secho(
            f"{os.path.basename(result.filepath)}: {status} (Wasted bits: {result.wasted_bits}/{result.bitdepth})",
            fg=color,
            bold=result.is_upconverted,
        )
