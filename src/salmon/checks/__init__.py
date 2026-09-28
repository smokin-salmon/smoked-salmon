import os

import asyncclick as click

from salmon.checks.integrity import handle_integrity_check
from salmon.checks.logs import check_log_cambia
from salmon.checks.upconverts import test_upconverted
from salmon.common import commandgroup
from salmon.common.files import process_files
from salmon.errors import CRCMismatchError, EditedLogError, LogCheckSkipped


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
        await _check_log(path, os.path.dirname(path))
    elif os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                if f.lower().endswith(".log"):
                    filepath = os.path.join(root, f)
                    click.secho(f"\nScoring {filepath}...", fg="cyan")
                    await _check_log(filepath, path)


async def _check_log(path: str, basepath: str) -> None:
    """Score a single log file and display the result.

    Args:
        path: Path to the log file to check.
        basepath: Release folder to search for the log's audio, as the upload does.
    """
    try:
        await check_log_cambia(path, basepath)
    except EditedLogError:
        click.secho("Error: Edited logs detected!", fg="red", bold=True)
    except CRCMismatchError:
        click.secho("Error: CRC mismatch between log and audio files!", fg="red", bold=True)
    except LogCheckSkipped as e:
        click.secho(f"Log not checked: {e}", fg="yellow")
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
async def mqa(path):
    """Check if a FLAC file is MQA"""
    # salmon.checks.mqa loads numpy: import it when an MQA check runs, not when salmon starts.
    from salmon.checks.mqa import check_mqa

    if os.path.isfile(path):
        if await check_mqa(path):
            click.secho("MQA syncword present", fg="red")
        else:
            click.secho("Did not find MQA syncword", fg="green")
    elif os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    filepath = os.path.join(root, f)
                    click.secho(f"\nChecking {filepath}...", fg="cyan")
                    if await check_mqa(filepath):
                        click.secho("MQA syncword present", fg="red")
                    else:
                        click.secho("Did not find MQA syncword", fg="green")


async def mqa_test(path: str) -> None:
    """Check if a FLAC file or any FLAC file in a directory contains MQA content.

    The files are checked concurrently, like the integrity check.

    Args:
        path: Path to the FLAC file or directory to check.

    Raises:
        click.Abort: If MQA syncword is detected.
    """
    from salmon.checks.mqa import check_mqa

    if os.path.isfile(path):
        filepaths = [path]
    elif os.path.isdir(path):
        filepaths = sorted(
            os.path.join(root, f) for root, _, files in os.walk(path) for f in files if f.lower().endswith(".flac")
        )
    else:
        return

    async def check(filepath: str, _: int) -> bool:
        return await check_mqa(filepath)

    detected = await process_files(filepaths, check, "Checking for MQA")
    hits = [filepath for filepath, found in zip(filepaths, detected, strict=True) if found]
    for filepath in hits:
        click.secho(f"MQA syncword present in '{filepath}'", fg="red", bold=True)
    if hits:
        raise click.Abort
