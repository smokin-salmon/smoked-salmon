import os
import re
from pathlib import Path

import click

from salmon import config
from salmon.converter.m3ercat import transcode

THREADS = [None] * config.SIMULTANEOUS_THREADS

FLAC_FOLDER_REGEX = re.compile(r"(24 ?bit )?FLAC", flags=re.IGNORECASE)
LOSSLESS_FOLDER_REGEX = re.compile(r"Lossless", flags=re.IGNORECASE)
LOSSY_EXTENSION_LIST = {
    ".mp3",
    ".m4a",  # Fuck ALAC.
    ".ogg",
    ".opus",
}


def transcode_folder(path, bitrate):
    _validate_folder_is_lossless(path)
    new_path = _generate_transcode_path_name(path, bitrate)
    if os.path.isdir(new_path):
        return click.secho(
            f"{new_path} already exists, please delete it to re-transcode.", fg="red"
        )
    transcode(Path(path), [bitrate], Path(new_path))

    return new_path


def _validate_folder_is_lossless(path):
    for _root, _, files in os.walk(path):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in LOSSY_EXTENSION_LIST:
                click.secho(f"A lossy file was found in the folder ({f}).", fg="red")
                raise click.Abort


def _generate_transcode_path_name(path, bitrate):
    to_append = []
    foldername = os.path.basename(path)
    if FLAC_FOLDER_REGEX.search(foldername):
        foldername = FLAC_FOLDER_REGEX.sub(f"MP3 {bitrate}", foldername)
    else:
        to_append.append(f"MP3 {bitrate}")

    if to_append:
        foldername += f' [{" ".join(to_append)}]'

    return os.path.join(os.path.dirname(path), foldername)


def generate_transcode_description(url, bitrate):

    lame_command = {"320":'-h -b 320 --ignore-tag-errors', "V0":"-V 0 --vbr-new --ignore-tag-errors"}[bitrate]

    description = f"[b]Source:[/b] {url}\n"
    description += f'[b]Transcode process:[/b] [code]flac -dcs -- input.flac | lame -S {lame_command} - output.mp3[/code]\n'

    return description