import os

import click

from salmon.checks.integrity import handle_integrity_check
from salmon.checks.logs import check_log_cambia
from salmon.checks.mqa import check_mqa
from salmon.checks.upconverts import test_upconverted
from salmon.common import commandgroup


@commandgroup.group()
def check():
    """Check/evaluate various aspects of files and folders"""
    pass


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
def log(path):
    """Check the score of log file(s)"""
    if os.path.isfile(path):
        _check_log(path)
    elif os.path.isdir(path):
        for root, _, figles in os.walk(path):
            for f in figles:
                if f.lower().endswith(".log"):
                    filepath = os.path.join(root, f)
                    click.secho(f"\nScoring {filepath}...", fg="cyan")
                    _check_log(filepath)


def _check_log(path):
    try:
        check_log_cambia(path, os.path.dirname(path))
    except Exception as e:
        if "Edited logs!" in str(e):
            click.secho("Error: Edited logs detected!", fg="red", bold=True)
        elif "CRC Mismatch!" in str(e):
            click.secho("Error: CRC mismatch between log and audio files!", fg="red", bold=True)
        else:
            click.secho(f"Error checking log: {e}", fg="red")


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
def upconv(path):
    """Check a 24bit FLAC file for upconversion"""
    test_upconverted(path)


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
def integrity(path):
    """Check the integrity of audio files"""
    handle_integrity_check(path)


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
        for root, _, figles in os.walk(path):
            for f in figles:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    filepath = os.path.join(root, f)
                    click.secho(f"\nChecking {filepath}...", fg="cyan")
                    if check_mqa(filepath):
                        click.secho("MQA syncword present", fg="red")
                    else:
                        click.secho("Did not find MQA syncword", fg="green")


def mqa_test(path):
    """Check if a FLAC file is MQA"""
    if os.path.isfile(path):
        if check_mqa(path):
            click.secho("MQA syncword present in '{path}'", fg="red", bold=True)
            raise click.Abort
        else:
            return False
    elif os.path.isdir(path):
        for root, _, figles in os.walk(path):
            for f in figles:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    filepath = os.path.join(root, f)
                    """ Only check the first file """
                    return bool(check_mqa(filepath))
