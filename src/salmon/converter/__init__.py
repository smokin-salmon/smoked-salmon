from typing import TYPE_CHECKING, cast

import asyncclick as click

from salmon.common import commandgroup

if TYPE_CHECKING:
    from salmon.converter.transcoding import Bitrate

BITRATES = ("V0", "320")


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True), nargs=1)
@click.option(
    "--bitrate",
    "-b",
    type=click.Choice(BITRATES, case_sensitive=False),
    required=True,
    help=f"Bitrate to transcode to ({', '.join(BITRATES)})",
)
@click.option(
    "--essential-only",
    "-eo",
    is_flag=True,
    help="Only keep music and image files; skip cues, logs and other extra files.",
)
async def transcode(path: str, bitrate: str, essential_only: bool) -> None:
    """Transcode a dir of FLACs into "perfect" MP3.

    Args:
        path: Path to the directory containing FLAC files.
        bitrate: Target bitrate (V0 or 320).
        essential_only: Only keep music and image files.
    """
    from salmon.converter.transcoding import transcode_folder

    await transcode_folder(path, cast("Bitrate", bitrate), essential_only=essential_only)


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
    from salmon.converter.downconverting import convert_folder

    await convert_folder(path, essential_only=essential_only)
