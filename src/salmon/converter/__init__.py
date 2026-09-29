import os
import re
from pathlib import Path
from typing import get_args

import asyncclick as click

from salmon import cfg
from salmon.common import commandgroup
from salmon.converter.downconverting import convert_folder
from salmon.converter.transcoding import Bitrate, transcode_folder


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True), nargs=1)
@click.option(
    "--bitrate",
    "-b",
    type=click.Choice(get_args(Bitrate), case_sensitive=False),
    required=True,
    help=f"Bitrate to transcode to ({', '.join(get_args(Bitrate))})",
)
@click.option(
    "--essential-only",
    "-eo",
    is_flag=True,
    help="Only keep music and image files; skip cues, logs and other extra files.",
)
async def transcode(path: str, bitrate: Bitrate, essential_only: bool) -> None:
    """Transcode a dir of FLACs into "perfect" MP3.

    Args:
        path: Path to the directory containing FLAC files.
        bitrate: Target bitrate (V0 or 320).
        essential_only: Only keep music and image files.
    """
    await transcode_folder(path, bitrate, essential_only=essential_only, output_dir=_output_dir(path))


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True), nargs=1)
@click.option(
    "--essential-only",
    "-eo",
    is_flag=True,
    help="Only keep music and image files; skip cues, logs and other extra files.",
)
async def downconv(path: str, essential_only: bool) -> None:
    """Downconvert a dir of 24bit FLACs to 16bit.

    Args:
        path: Path to the directory containing 24bit FLAC files.
        essential_only: Only keep music and image files.
    """
    await convert_folder(path, essential_only=essential_only, output_dir=_output_dir(path))


def _output_dir(path: str) -> str | None:
    """Where the converted folder goes: beside the source, or for a library album, under download_directory.

    The album's resolved parent path is mirrored there (/data/music/Artist/Album converts into
    download_directory/data/music/Artist), so albums of the same name in different folders, or in different
    libraries, never share an output folder, and a rerun finds its own.
    """
    if not cfg.directory.is_library_path(path):
        return None
    parent = Path(os.path.realpath(path)).parent
    # The drive or share on Windows, as a plain folder name; nothing on POSIX.
    anchor = re.sub(r"\W", "", parent.anchor)
    output_dir = os.path.join(cfg.directory.download_directory, anchor, *parent.parts[1:])
    click.secho(f"{path} is in library_dirs: writing the output into {output_dir}.", fg="yellow")
    return output_dir
