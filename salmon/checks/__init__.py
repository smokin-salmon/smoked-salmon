import os

import asyncclick as click

from salmon.checks.integrity import handle_integrity_check
from salmon.checks.logs import check_log_cambia
from salmon.checks.mqa import check_mqa
from salmon.checks.upconverts import test_upconverted
from salmon.common import commandgroup
from salmon.errors import CRCMismatchError, EditedLogError


@commandgroup.group()
def check():
    """Check/evaluate various aspects of files and folders"""
    pass


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
async def log(path: str) -> None:
    """Check the score of log file(s).

    Args:
        path: Path to a log file or directory containing log files.
    """
    if os.path.isfile(path):
        await _check_log(path)
    elif os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                if f.lower().endswith(".log"):
                    filepath = os.path.join(root, f)
                    click.secho(f"\nScoring {filepath}...", fg="cyan")
                    await _check_log(filepath)


async def _check_log(path: str) -> None:
    """Score a single log file and display the result.

    Args:
        path: Path to the log file to check.
    """
    try:
        await check_log_cambia(path, os.path.dirname(path))
    except EditedLogError:
        click.secho("Error: Edited logs detected!", fg="red", bold=True)
    except CRCMismatchError:
        click.secho("Error: CRC mismatch between log and audio files!", fg="red", bold=True)
    except Exception as e:
        click.secho(f"Error checking log: {e}", fg="red")


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
async def upconv(path: str) -> None:
    """Check a 24bit FLAC file for upconversion.

    Args:
        path: Path to the FLAC file or directory to check.
    """
    await test_upconverted(path)


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
async def integrity(path: str) -> None:
    """Check the integrity of audio files.

    Args:
        path: Path to the audio file or directory to check.
    """
    await handle_integrity_check(path)


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
def mqa(path):
    """Check if a FLAC file is MQA"""
    if os.path.isfile(path):
        if check_mqa(path):
            click.secho("MQA syncword present", fg="red")
        else:
            click.secho("Did not find MQA syncword", fg="green")
    elif os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    filepath = os.path.join(root, f)
                    click.secho(f"\nChecking {filepath}...", fg="cyan")
                    if check_mqa(filepath):
                        click.secho("MQA syncword present", fg="red")
                    else:
                        click.secho("Did not find MQA syncword", fg="green")


def mqa_test(path: str) -> bool | None:
    """Check if a FLAC file or directory contains MQA content.

    Args:
        path: Path to the FLAC file or directory to check.

    Returns:
        False if no MQA detected in a single file, True/False for
        the first file in a directory, or None if path is invalid.

    Raises:
        click.Abort: If MQA syncword is detected in a single file.
    """
    if os.path.isfile(path):
        if check_mqa(path):
            click.secho(f"MQA syncword present in '{path}'", fg="red", bold=True)
            raise click.Abort
        else:
            return False
    elif os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    filepath = os.path.join(root, f)
                    # Only check the first file
                    return bool(check_mqa(filepath))
