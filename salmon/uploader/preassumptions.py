import asyncio
from html import unescape

import click

from salmon import cfg
from salmon.errors import RequestError, UploadError


def print_preassumptions(gazelle_site, path, group_id, source, lossy, spectrals, encoding, spectrals_after):
    """Print what all the passed CLI options will do."""
    click.secho(f"\nProcessing {path}", fg="cyan", bold=True)
    second = []
    if source:
        second.append(f"from {source}")
    if list(encoding) != [None, None]:
        text = f"as {encoding[0]}"
        if encoding[1]:
            text += " (VBR)"
        second.append(text)
    if lossy is not None:
        second.append(f"with lossy master status as {lossy}")
    if second:
        click.secho(f"Uploading {' '.join(second)}.", fg="yellow")
    if spectrals:
        if spectrals == (0,):
            click.secho("Uploading no spectrals.", fg="yellow")
        else:
            click.secho(
                f"Uploading spectrals {', '.join(str(s) for s in spectrals)}.",
                fg="yellow",
            )
    if spectrals_after:
        click.secho(
            "Assessing spectrals after upload.",
            fg="yellow",
        )

    if lossy and not spectrals:
        raise UploadError("\nYou cannot report a torrent for lossy master without spectrals.")

    if group_id:
        print_group_info(gazelle_site, group_id, source)
        click.confirm(
            click.style("\nWould you like to continue to upload to this group?", fg="magenta"),
            default=True,
            abort=True,
        )


def print_group_info(gazelle_site, group_id, source):
    """
    Print information about the torrent group that was passed as a CLI argument.
    Also print all the torrents that are in that group.
    """
    try:
        group = asyncio.run(gazelle_site.torrentgroup(group_id))
    except RequestError as err:
        raise UploadError("Could not get information about torrent group from RED.") from err

    artists = [a["name"] for a in group["group"]["musicInfo"]["artists"]]
    artists = ", ".join(artists) if len(artists) < 4 else cfg.upload.formatting.various_artist_word
    click.secho(
        f"\nTorrents matching source {source} in (Group {group_id}) {artists} - {group['group']['name']}:",
        fg="yellow",
        bold=True,
    )

    for t in group["torrents"]:
        if t["media"] == source:
            if t["remastered"]:
                click.echo(
                    unescape(
                        f"> {t['remasterYear']} / {t['remasterRecordLabel']} / "
                        f"{t['remasterCatalogueNumber']} / {t['format']} / "
                        f"{t['encoding']}"
                    )
                )
            if not t["remastered"]:
                click.echo(
                    unescape(
                        f"> OR / {group['group']['recordLabel']} / "
                        f"{group['group']['catalogueNumber']} / {t['format']} / "
                        f"{t['encoding']}"
                    )
                )
