import os
from pathlib import Path

import click
from heybrochecklog import format_score, format_translation
from heybrochecklog.score import score_log
from heybrochecklog.translate import translate_log

from salmon.checks.integrity import handle_integrity_check
from salmon.checks.mqa import check_mqa
from salmon.checks.upconverts import test_upconverted
from salmon.common import commandgroup


@commandgroup.group()
def check():
    """Check/evaluate various aspects of files and folders"""
    pass


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
@click.option("--score-only", "-s", is_flag=True, help="Print only the score")
@click.option(
    "--translate", "-t", is_flag=True, help="Translate and print log alongside score"
)
def log(path, score_only, translate):
    """Check the score of (and translate) log file(s)"""
    if os.path.isfile(path):
        _check_log(path, score_only, translate)
    elif os.path.isdir(path):
        for root, _, figles in os.walk(path):
            for f in figles:
                if f.lower().endswith(".log"):
                    filepath = os.path.join(root, f)
                    click.secho(f"\nScoring {path}...", fg="cyan")
                    _check_log(filepath, score_only, translate)


def _check_log(path, score_only, translate):
    figle = Path(path)
    scored_log = score_log(figle, markup=False)
    if score_only:
        if scored_log["unrecognized"]:
            return click.secho("Unrecognized")
        return click.echo(scored_log["score"])

    try:
        click.echo(format_score(path, scored_log, markup=False))
        if translate:
            translated_log = translate_log(figle)
            click.secho(
                "\n---------------------------------------------------\n"
                + format_translation(path, translated_log)
            )
    except UnicodeEncodeError as e:
        click.secho(f"Could not encode logpath: {e}")


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
