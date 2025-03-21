import os
import re
import shlex
import subprocess
import time
from copy import copy
from shutil import copyfile

import click

from salmon import config
from salmon.errors import InvalidSampleRate
from salmon.tagger.audio_info import gather_audio_info

THREADS = [None] * config.SIMULTANEOUS_CONVERSIONS
COMMAND = "sox {input_} -G -b 16 {output} rate -v -L {rate} dither"
FLAC_FOLDER_REGEX = re.compile(r"(24 ?bit )?FLAC", flags=re.IGNORECASE)


def convert_folder(path):
    new_path = _generate_conversion_path_name(path)
    if os.path.isdir(new_path):
        return click.secho(
            f"{new_path} already exists, please delete it to re-convert.", fg="red"
        )

    files_convert, files_copy = _determine_files_actions(path)
    _convert_files(path, new_path, files_convert, files_copy)


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


def _convert_files(old_path, new_path, files_convert, files_copy):
    files_left = len(files_convert) - 1
    files = iter(files_convert)

    for file_ in files_copy:
        output = file_.replace(old_path, new_path)
        _create_path(output)
        copyfile(file_, output)
        click.secho(f"Copied {os.path.basename(file_)}")

    while True:
        for i, thread in enumerate(THREADS):
            if thread and thread.poll() is not None:  # Process finished
                exit_code = thread.returncode
                if exit_code != 0:  # Error handling
                    stderr_output = thread.communicate()[1].decode("utf-8", "ignore")
                    click.secho(f"Error downconverting a file, error {exit_code}:", fg="red")
                    click.secho(stderr_output)
                    raise click.Abort  # Consider collecting errors instead of aborting

                # Process is finished, and there was no error
                THREADS[i] = None  # Mark the slot as free

            if THREADS[i] is None:  # If thread is free, assign new file
                try:
                    file_, sample_rate = next(files)
                except StopIteration:
                    THREADS[i] = None
                else:
                    output = file_.replace(old_path, new_path)
                    THREADS[i] = _convert_single_file(file_, output, sample_rate, files_left)
                    files_left -= 1

        if all(t is None for t in THREADS):     # No active threads and no more files
            break
        time.sleep(0.1)


def _convert_single_file(file_, output, sample_rate, files_left):
    click.echo(f"Converting {os.path.basename(file_)} [{files_left} left to convert]")
    _create_path(output)
    command = COMMAND.format(
        input_=shlex.quote(file_),
        output=shlex.quote(output),
        rate=_get_final_sample_rate(sample_rate),
    )
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
    )


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
