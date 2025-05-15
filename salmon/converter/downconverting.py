import os
import re
import subprocess
import time
from copy import copy
from shutil import copyfile

import click

from salmon import config
from salmon.errors import InvalidSampleRate
from salmon.tagger.audio_info import gather_audio_info

THREADS = [None] * config.SIMULTANEOUS_THREADS
FLAC_FOLDER_REGEX = re.compile(r"(24 ?bit )?FLAC", flags=re.IGNORECASE)


def convert_folder(path, override_sample_rate=None):

    new_path = _generate_conversion_path_name(path)
    if override_sample_rate:
        new_path = re.sub(
            "FLAC",
            f"24-{override_sample_rate / 1000:.0f}",
            new_path,
            flags=re.IGNORECASE,
        )
    if os.path.isdir(new_path):
        return click.secho(
            f"{new_path} already exists, please delete it to re-convert.", fg="red"
        )

    files_convert, files_copy = _determine_files_actions(path)
    sample_rate = _convert_files(
        path, new_path, files_convert, files_copy, override_sample_rate
    )

    return sample_rate, new_path


def _determine_files_actions(path):
    convert_files = []
    copy_files = [os.path.join(r, f) for r, _, files in os.walk(path) for f in files]
    audio_info = gather_audio_info(path)
    for figle in copy(copy_files):
        for info_figle, figle_info in audio_info.items():
            if figle.endswith(info_figle) and figle_info["precision"] == 24:
                convert_files.append((figle, figle_info["sample rate"]))
                copy_files.remove(figle)
    return convert_files, copy_files


def _generate_conversion_path_name(path):
    foldername = os.path.basename(path)
    if re.search("24 ?bit FLAC", foldername, flags=re.IGNORECASE):
        foldername = re.sub("24 ?bit FLAC", "FLAC", foldername, flags=re.IGNORECASE)
    elif re.search("FLAC", foldername, flags=re.IGNORECASE):
        foldername = re.sub("FLAC", "16bit FLAC", foldername, flags=re.IGNORECASE)
    else:
        foldername += " [FLAC]"

    return os.path.join(os.path.dirname(path), foldername)


def _convert_files(
    old_path, new_path, files_convert, files_copy, override_sample_rate=None
):
    files_left = len(files_convert) - 1
    files = iter(files_convert)

    for file_ in files_copy:
        output = file_.replace(old_path, new_path)
        _create_path(output)
        copyfile(file_, output)
        click.secho(f"Copied {os.path.basename(file_)}")

    converting = True
    while converting:
        converting = False
        for i, thread in enumerate(THREADS):
            if thread and thread.poll() is not None:
                if thread.poll() != 0:
                    click.secho(
                        f"Error downconverting a file, error {thread.poll()}:", fg="red"
                    )
                    click.secho(thread.communicate()[1].decode("utf-8", "ignore"))
                    raise click.Abort
                try:
                    thread.kill()
                except:  # noqa: E722
                    pass

            if not thread or thread.poll() is not None:
                try:
                    file_, sample_rate = next(files)
                except StopIteration:
                    break

                output = file_.replace(old_path, new_path)
                THREADS[i] = _convert_single_file(
                    file_, output, sample_rate, files_left, override_sample_rate
                )
                files_left -= 1
            converting = True
        time.sleep(0.1)

    return override_sample_rate if override_sample_rate else _get_final_sample_rate(sample_rate)


def _convert_single_file(
    file_, output, sample_rate, files_left, override_sample_rate=None
):
    click.echo(f"Converting {os.path.basename(file_)} [{files_left} left to convert]")
    _create_path(output)

    command = [
        "sox",
        file_,
        "-R",
        "-G",
        *(["-b", "16"] if not override_sample_rate else []),
        output,
        "rate",
        "-v",
        "-L",
        (
            str(override_sample_rate)
            if override_sample_rate
            else str(_get_final_sample_rate(sample_rate))
        ),
        "dither",
    ]

    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _create_path(filepath):
    p = os.path.dirname(filepath)
    if not os.path.isdir(p):
        try:
            os.makedirs(p)
        except FileExistsError:
            pass


def _get_final_sample_rate(sample_rate):
    if sample_rate % 44100 == 0:
        return 44100
    elif sample_rate % 48000 == 0:
        return 48000
    raise InvalidSampleRate


def generate_conversion_description(url, sample_rate):

    description = ""

    if sample_rate <= 48000:
        description += (
            f"Encode Specifics: 16 bit {sample_rate / 1000:.01f} kHz\n"
            f"[b]Source:[/b] {url}\n"
            f"[b]Transcode process:[/b] [code]sox input.flac -R -G -b 16 output.flac rate -v -L {sample_rate} dither[/code]\n"
        )
    else:
        description += (
            f"Encode Specifics: 24 bit {sample_rate / 1000:.01f} kHz\n"
            f"[b]Source:[/b] {url}\n"
            f"[b]Transcode process:[/b] [code]sox input.flac -R -G output.flac rate -v -L {sample_rate} dither[/code]\n"
        )

    return description
