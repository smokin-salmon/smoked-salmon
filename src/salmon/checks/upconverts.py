import math
import os
import re
from functools import partial

import anyio
import asyncclick as click
import msgspec
from mutagen import MutagenError, flac

from salmon import interaction
from salmon.common.files import process_files
from salmon.errors import UpconvertCheckError, UpconvertCheckNotApplicable


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
        if await interaction.confirm(
            click.style(
                "Possible upconverts detected. Would you like to quit uploading?",
                fg="red",
            ),
            default=True,
        ):
            raise click.Abort
    elif any_upconverts is False:
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
    skipped: list[str] = []
    if os.path.isfile(path):
        result = await _upconvert_check_handler(path, skipped=skipped)
        if result is None:
            _say_if_all_skipped(skipped, 1)
            return None
        _display_results([result])
        return result.is_upconverted
    elif os.path.isdir(path):
        flac_files = []
        for root, _, files in os.walk(path):
            for f in files:
                if f.lower().endswith(".flac"):
                    flac_files.append(os.path.join(root, f))
        if not flac_files:
            click.secho("No FLAC files found to check.", fg="yellow")
        results = await process_files(
            flac_files,
            partial(_upconvert_check_handler, skipped=skipped),
            "Checking FLAC files for upconverts",
        )
        valid_results = [r for r in results if r is not None]
        _say_if_all_skipped(skipped, len(flac_files))
        _display_results(valid_results)
        return any(r.is_upconverted for r in valid_results) if valid_results else None
    return None


def _say_if_all_skipped(skipped: list[str], total: int) -> None:
    """One line, not one per file, when every file was 16bit and so left out of the check."""
    if skipped and len(skipped) == total:
        click.secho("Nothing to check: the upconvert check skips 16bit FLAC files, and these all are.", fg="yellow")


async def _upconvert_check_handler(
    filepath: str, _: int | None = None, skipped: list[str] | None = None
) -> UpconvertCheckResult | None:
    """Handle upconvert check for a single file, logging errors.

    Args:
        filepath: Path to the FLAC file to check.
        _: Unused index parameter for process_files compatibility.
        skipped: Collects the files left out because they are 16bit.

    Returns:
        UpconvertCheckResult on success, None if the file is out of scope or cannot be analyzed.
    """
    try:
        return await check_upconvert(filepath)
    except UpconvertCheckNotApplicable:
        # 16bit is out of scope, so it is not worth a line of warning per track.
        if skipped is not None:
            skipped.append(filepath)
        return None
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
        UpconvertCheckNotApplicable: If the file is 16bit.
        UpconvertCheckError: If the file cannot be analyzed.
    """
    try:
        mut = flac.FLAC(filepath)
        bitdepth = mut.info.bits_per_sample
    except MutagenError as e:
        raise UpconvertCheckError("This is not a FLAC file.") from e

    if bitdepth == 16:
        raise UpconvertCheckNotApplicable("This is a 16bit FLAC file.")

    try:
        response = await anyio.run_process(["flac", "-ac", filepath], check=False)
    except FileNotFoundError as e:
        raise UpconvertCheckError(f"flac binary not found: {e}") from e
    if response.returncode != 0:
        raise UpconvertCheckError(f"File appears to be corrupt: {response.stderr.decode().strip()}")
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
