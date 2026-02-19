import math
import os
import re
import subprocess

import asyncclick as click
from mutagen import MutagenError, flac

from salmon.common.figles import process_files
from salmon.errors import NotAValidInputFile


def upload_upconvert_test(path):
    any_upconverts = test_upconverted(path)
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


def test_upconverted(path):
    if os.path.isfile(path):
        result = _upconvert_check_handler(path)
        _display_results([result])
        return result[0]
    elif os.path.isdir(path):
        flac_files = []
        for root, _, files in os.walk(path):
            for f in files:
                if f.lower().endswith(".flac"):
                    flac_files.append(os.path.join(root, f))
        results = process_files(flac_files, _upconvert_check_handler, "Checking FLAC files for upconverts")
        _display_results(results)
        return any(r[0] for r in results if r[0] is not None)


def _upconvert_check_handler(filepath, _=None):
    try:
        upconv, wasted_bits, bitdepth, error = check_upconvert(filepath)
        return upconv, wasted_bits, bitdepth, filepath, error
    except NotAValidInputFile as e:
        return None, None, None, filepath, "Unable to check file: " + str(e)


def check_upconvert(filepath):
    try:
        mut = flac.FLAC(filepath)
        bitdepth = mut.info.bits_per_sample
    except MutagenError:
        return None, None, None, "This is not a FLAC file."

    if bitdepth == 16:
        return None, None, bitdepth, "This is a 16bit FLAC file."

    with open(os.devnull, "w") as devnull:
        response = subprocess.check_output(["flac", "-ac", filepath], stderr=devnull, text=True)

    wasted_bits_list = []
    for line in response.split("\n"):
        r = re.search(r"wasted_bits=(\d+)", line)
        if r:
            wasted_bits_list.append(int(r[1]))

    wasted_bits = math.ceil(sum(wasted_bits_list) / len(wasted_bits_list))
    if wasted_bits >= 8:
        return True, wasted_bits, bitdepth, None
    else:
        return False, wasted_bits, bitdepth, None


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


def _display_results(results):
    sorted_results = sorted(results, key=lambda x: _tracknumber_sort_key(x[3]))

    for upconv, wasted_bits, bitdepth, filepath, error in sorted_results:
        if upconv is None:
            click.secho(f"{os.path.basename(filepath)}: {error}", fg="yellow")
        else:
            status = "likely upconverted" if upconv else "does not have a high number of wasted bits"
            color = "red" if upconv else "green"
            click.secho(
                f"{os.path.basename(filepath)}: {status} (Wasted bits: {wasted_bits}/{bitdepth})", fg=color, bold=upconv
            )
