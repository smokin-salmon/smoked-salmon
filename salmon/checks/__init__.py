import os
from pathlib import Path
import subprocess
import click
import json
from more_itertools import windowed
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


def is_sublist(sub, main):
    return any(tuple(sub) == window for window in windowed(main, len(sub)))


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


def check_log_cambia(logpath, basepath):
    figle = Path(logpath)
    try:
        cambia_output = json.loads(
            subprocess.check_output(
                ["cambia", "-p", logpath],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
        )
        click.echo(
            f"Log Score: {cambia_output['evaluation_combined'][0]['combined_score']}"
        )
    except Exception as e:
        click.secho(f"Error checking log {logpath}: {e}")

    if cambia_output['parsed']['parsed_logs'][0]['checksum']['integrity'] == "Mismatch":
        raise ValueError("Edited logs!")
    elif cambia_output['parsed']['parsed_logs'][0]['checksum']['integrity'] == 'Unknown':
        click.secho(f"Lacking a valid checksum. The torrent will be marked as trumpable.", fg="cyan")

    copy_crc_list = [
        track['test_and_copy']['copy_hash']
        for track in cambia_output['parsed']['parsed_logs'][0]['tracks']
    ]

    crc_list = []
    for root, folders, files_ in os.walk(basepath):
        for f in files_:
            if os.path.splitext(f.lower())[1] in {".flac", ".mp3", ".m4a"}:
                output = subprocess.check_output(
                    [
                        "ffmpeg",
                        "-i",
                        os.path.join(root, f),
                        "-map",
                        "0:0",
                        "-f",
                        "hash",
                        "-hash",
                        "crc32",
                        "-",
                    ],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                )
                crc_list.append(output.strip().removeprefix("CRC32=").upper())

    if not is_sublist(sub=copy_crc_list, main=crc_list):
        raise ValueError("CRC Mismatch!")
        print


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
