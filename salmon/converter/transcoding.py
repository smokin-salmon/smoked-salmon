import contextlib
import os
import re
import shlex
import subprocess
import time
from shutil import copyfile

import click
import mutagen

from salmon import cfg

THREADS = [None] * cfg.upload.simultaneous_threads
COMMANDS = {
    "320": "flac --decode --stdout {input_} | lame -b 320 -q 0 --add-id3v2 "
        "--tt {tt} --ta {ta} --ty {ty} --tn {tn} --tl {tl} --tc {tc} --tg {tg} "
        "--tv TPUB={label} - {output}",
    "V0": "flac --decode --stdout {input_} | lame -V 0 -q 0 --add-id3v2 "
        "--tt {tt} --ta {ta} --ty {ty} --tn {tn} --tl {tl} --tc {tc} --tg {tg} "
        "--tv TPUB={label} - {output}",
}
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
    _transcode_files(path, new_path, bitrate)


def _validate_folder_is_lossless(path):
    for _root, _, files in os.walk(path):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in LOSSY_EXTENSION_LIST:
                click.secho(f"A lossy file was found in the folder ({f}).", fg="red")
                raise click.Abort


def _get_files_to_handle(path):
    files_to_handle = []
    for root, _, files in os.walk(path):
        for f in files:
            files_to_handle.append(os.path.join(root, f))
    return files_to_handle


def _generate_transcode_path_name(path, bitrate):
    to_append = []
    foldername = os.path.basename(path)
    if FLAC_FOLDER_REGEX.search(foldername):
        foldername = FLAC_FOLDER_REGEX.sub("MP3", foldername)
    else:
        to_append.append("MP3")
    if LOSSLESS_FOLDER_REGEX.search(foldername):
        foldername = LOSSLESS_FOLDER_REGEX.sub(bitrate, foldername)
    else:
        to_append.append(bitrate)

    if to_append:
        foldername += f' [{" ".join(to_append)}]'

    return os.path.join(os.path.dirname(path), foldername)


def _transcode_files(old_path, new_path, bitrate):
    files = _get_files_to_handle(old_path)
    files_left = len([f for f in files if f.lower().endswith(".flac")]) - 1
    files = iter(sorted(files))
    
    has_pending_files = True
    while has_pending_files or any(t is not None for t in THREADS):
        # Process completed threads
        for i, thread in enumerate(THREADS):
            if thread and thread.poll() is not None:
                if thread.poll() != 0:
                    click.secho(
                        f"Error transcoding a file, error {thread.poll()}:", fg="red"
                    )
                    click.secho(thread.communicate()[1].decode("utf-8", "ignore"))
                    raise click.Abort
                with contextlib.suppress(Exception):
                    thread.kill()
                THREADS[i] = None

            # Start new transcoding if this thread slot is free and we have files
            if (not thread or thread.poll() is not None) and has_pending_files:
                try:
                    file_ = next(files)
                except StopIteration:
                    has_pending_files = False
                    continue

                output = file_.replace(old_path, new_path)
                if file_.lower().endswith(".flac"):
                    output = re.sub(r".flac$", ".mp3", output, flags=re.IGNORECASE)
                    THREADS[i] = _transcode_single_file(
                        file_, output, bitrate, files_left
                    )
                    files_left -= 1
                else:
                    _create_path(output)
                    copyfile(file_, output)
                    click.secho(f"Copied {os.path.basename(file_)}")
        time.sleep(0.1)


def _transcode_single_file(file_, output, bitrate, files_left):
    click.echo(
        f"Transcoding {os.path.basename(file_)} [{files_left} left to transcode]"
    )
    _create_path(output)
    try:
        command = COMMANDS[bitrate].format(
            input_=shlex.quote(file_), output=shlex.quote(output)
        )
    except KeyError:
        command = COMMANDS[bitrate].format(
            input_=shlex.quote(file_), output=shlex.quote(output), **_get_tags(file_)
        )
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
    )


def _create_path(filepath):
    p = os.path.dirname(filepath)
    if not os.path.isdir(p):
        with contextlib.suppress(FileExistsError):
            os.makedirs(p)


def _get_tags(file_):
    tags = {}
    tag_assignments = {
        "tt": ["title"],
        "ta": ["artist"],
        "tl": ["album"],
        "ty": ["date", "year"],
        "tn": ["tracknumber"],
        "tc": ["comment"],
        "tg": ["genre"],
        "label": ["label"],
    }

    track = mutagen.File(file_)
    for key, tag_keys in tag_assignments.items():
        for tag_key in tag_keys:
            try:
                tags[key] = shlex.quote(track.tags[tag_key][0])
            except (KeyError, IndexError):
                if key not in tags or tags[key] != "''":
                    tags[key] = "''"
    return tags
