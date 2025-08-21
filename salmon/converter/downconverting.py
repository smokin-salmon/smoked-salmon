import contextlib
import os
import re
import subprocess
import time
from copy import copy
from shutil import copyfile

import click

from salmon import cfg
from salmon.errors import InvalidSampleRate
from salmon.tagger.audio_info import gather_audio_info

THREADS = [None] * cfg.upload.simultaneous_threads
FLAC_FOLDER_REGEX = re.compile(r"(24 ?bit )?FLAC", flags=re.IGNORECASE)


def convert_folder(path, bit_depth=16, sample_rate=None):
    new_path = _generate_conversion_path_name(path)
    if sample_rate and bit_depth == 24:
        new_path = re.sub(
            "FLAC",
            f"24-{sample_rate / 1000:.0f}",
            new_path,
            flags=re.IGNORECASE,
        )
    if os.path.isdir(new_path):
        click.secho(f"{new_path} already exists.", fg="yellow")
        return sample_rate, new_path

    files_convert, files_copy = _determine_files_actions(path)
    final_sample_rate = _convert_files(path, new_path, files_convert, files_copy, bit_depth, sample_rate)

    return final_sample_rate, new_path


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


def _convert_files(old_path, new_path, files_convert, files_copy, bit_depth=16, sample_rate=None):
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
                    file_, original_sample_rate = next(files)
                except StopIteration:
                    THREADS[i] = None
                else:
                    output = file_.replace(old_path, new_path)
                    final_sample_rate = sample_rate if sample_rate else _get_final_sample_rate(original_sample_rate)
                    THREADS[i] = _convert_single_file(
                        file_,
                        output,
                        files_left,
                        bit_depth,
                        final_sample_rate,
                    )
                    files_left -= 1

        if all(t is None for t in THREADS):  # No active threads and no more files
            break
        time.sleep(0.1)

    return final_sample_rate


def _convert_single_file(file_, output, files_left, bit_depth=16, sample_rate=None):
    click.echo(f"Converting {os.path.basename(file_)} [{files_left} left to convert]")
    _create_path(output)

    command = [
        "sox",
        file_,
        "-R",
        "-G",
        *([] if bit_depth == 24 else ["-b", str(bit_depth)]),
        output,
        "rate",
        "-v",
        "-L",
        str(sample_rate),
        "dither",
    ]

    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _create_path(filepath):
    p = os.path.dirname(filepath)
    if not os.path.isdir(p):
        with contextlib.suppress(FileExistsError):
            os.makedirs(p, exist_ok=True)


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
            f"[b]Transcode process:[/b] "
            f"[code]sox input.flac -R -G -b 16 output.flac rate -v -L {sample_rate} dither[/code]\n"
        )
    else:
        description += (
            f"Encode Specifics: 24 bit {sample_rate / 1000:.01f} kHz\n"
            f"[b]Source:[/b] {url}\n"
            f"[b]Transcode process:[/b] [code]sox input.flac -R -G output.flac rate -v -L {sample_rate} dither[/code]\n"
        )

    return description
