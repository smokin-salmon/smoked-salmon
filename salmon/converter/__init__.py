import click

from salmon.common import commandgroup
from salmon.converter.downconverting import convert_folder
from salmon.converter.transcoding import transcode_folder

VALID_TRANSCODE_BITRATES = ["V0", "320"]


def validate_bitrate(ctx, param, value):
    if value.upper() in VALID_TRANSCODE_BITRATES:
        return value.upper()
    else:
        raise click.BadParameter(
            f"{value} is not a valid bitrate. Valid bitrates are: "
            + ", ".join(VALID_TRANSCODE_BITRATES)
        )


@commandgroup.command()
@click.argument(
    "path", type=click.Path(exists=True, file_okay=False, resolve_path=True), nargs=1
)
@click.option(
    "--bitrate",
    "-b",
    type=click.STRING,
    callback=validate_bitrate,
    required=True,
    help=f'Bitrate to transcode to ({", ".join(VALID_TRANSCODE_BITRATES)})',
)
@click.option(
    "--skip-unneeded-files",
    "-suf",
    is_flag=True,
    help='Extraneous files (for example scans, cues, logs) will be skipped when copying, leaving only music and cover art.'
)
def transcode(path, bitrate,skip_unneeded_files):
    """Transcode a dir of FLACs into "perfect" MP3"""
    transcode_folder(path, bitrate,skip_unneeded_files)


@commandgroup.command()
@click.argument(
    "path", type=click.Path(exists=True, file_okay=False, resolve_path=True), nargs=1
)
@click.option(
    "--skip-unneeded-files",
    "-suf",
    is_flag=True,
    help='Extraneous files (for example scans, cues, logs) will be skipped when copying, leaving only music and cover art.'
)
def downconv(path, skip_unneeded_files):
    """Downconvert a dir of 24bit FLACs to 16bit"""
    convert_folder(path, skip_unneeded_files)
