import os

import asyncclick as click
import mutagen

from salmon.common import compress, get_audio_files
from salmon.errors import UploadError


def gather_audio_info(path, sort_by_tracknumber=False):
    """
    Iterate over all audio files in the directory and parse the technical
    information about the files into a dictionary.
    """
    files = get_audio_files(path, sort_by_tracknumber)
    if not files:
        raise UploadError("No audio files found.")

    audio_info = {}
    for filename in files:
        mut = mutagen.File(os.path.join(path, filename))
        audio_info[filename] = _parse_audio_info(mut.info)
    return audio_info


def _parse_audio_info(streaminfo):
    return {
        "channels": streaminfo.channels,
        "sample rate": streaminfo.sample_rate,
        "bit rate": streaminfo.bitrate,
        "precision": getattr(streaminfo, "bits_per_sample", None),
        "duration": int(streaminfo.length),
    }


def check_hybrid(tags):
    """Check whether or not the release has mixed precisions/sample rate."""
    first_tag = next(iter(tags.values()))
    if not all(
        t["precision"] == first_tag["precision"] and t["sample rate"] == first_tag["sample rate"] for t in tags.values()
    ):
        click.secho(
            "Release has mixed bit depths / sample rates. Flagging as hybrid.",
            fg="yellow",
        )
        return True
    return False


def recompress_path(path):
    """Recompress all flacs in the directory to the configured compression level."""
    files = get_audio_files(path)
    if not files or not all(".flac" in f for f in files):
        return click.secho("No flacs found to recompress. Skipping...", fg="red")
    for filename in files:
        filepath = os.path.join(path, filename)
        compress(filepath)
