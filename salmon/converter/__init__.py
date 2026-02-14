import asyncclick as click

from salmon.common import commandgroup
from salmon.converter.downconverting import convert_folder
from salmon.converter.transcoding import transcode_folder

VALID_TRANSCODE_BITRATES = ["V0", "320"]


def validate_bitrate(ctx, param, value):
    if value.upper() in VALID_TRANSCODE_BITRATES:
        return value.upper()
    else:
        raise click.BadParameter(
            f"{value} is not a valid bitrate. Valid bitrates are: " + ", ".join(VALID_TRANSCODE_BITRATES)
        )


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True), nargs=1)
@click.option(
    "--bitrate",
    "-b",
    type=click.STRING,
    callback=validate_bitrate,
    required=True,
    help=f"Bitrate to transcode to ({', '.join(VALID_TRANSCODE_BITRATES)})",
)
def transcode(path, bitrate):
    """Transcode a dir of FLACs into "perfect" MP3"""
    transcode_folder(path, bitrate)


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True), nargs=1)
def downconv(path):
    """Downconvert a dir of 24bit FLACs to 16bit"""
    convert_folder(path)
