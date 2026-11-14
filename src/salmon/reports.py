"""Approval reports for torrents that are already on a tracker."""

from urllib.parse import parse_qs, urlsplit

import asyncclick as click

from salmon import interaction, trackers
from salmon.common import commandgroup
from salmon.common.strings import numeric_id
from salmon.errors import RequestError, UnknownOutcomeError
from salmon.uploader.spectrals import _add_spectral_links_to_lossy_comment, generate_lossy_approval_comment


def parse_target(target: str, tracker: str | None) -> tuple[int, str | None]:
    """Accept a positive torrent ID or a recognized torrent permalink."""
    value = target.strip()
    if "://" in value:
        try:
            url = urlsplit(value)
            code = trackers.tracker_url_code_map.get(url.netloc)
            ids = parse_qs(url.query, keep_blank_values=True).get("torrentid", [])
            if url.scheme not in ("http", "https") or code not in ("RED", "OPS"):
                raise ValueError
            if url.path != "/torrents.php" or len(ids) != 1:
                raise ValueError
        except ValueError:
            raise click.BadParameter("Use a RED or OPS torrent permalink containing one torrentid.") from None
        if tracker and tracker != code:
            raise click.BadParameter("The permalink and --tracker name different trackers.")
        tracker, value = code, ids[0]
    torrent_id = numeric_id(value)
    if torrent_id is None or torrent_id <= 0:
        raise click.BadParameter("Use a positive torrent ID or a RED or OPS torrent permalink.")
    return torrent_id, tracker


@commandgroup.group()
async def report() -> None:
    """Submit reports for existing torrents."""


@report.command()
@click.argument("target")
@click.option("--type", "report_type", type=click.Choice(["master", "web"]))
@click.option("--tracker", type=click.Choice(["RED", "OPS"], case_sensitive=False))
@click.option("--comment", help="Source information and evidence for the approval request.")
@click.option("--source-url", help="Release source URL.")
@click.option("--dry-run", "--print-only", is_flag=True, help="Print the report without contacting the tracker.")
async def lossy(
    target: str,
    report_type: str | None,
    tracker: str | None,
    comment: str | None,
    source_url: str | None,
    dry_run: bool,
) -> None:
    """Request lossy master/WEB approval for a torrent ID or permalink. No local audio is needed."""
    await run_lossy_report(target, report_type, tracker, comment, source_url, dry_run)


async def run_lossy_report(
    target: str,
    report_type: str | None,
    tracker: str | None,
    comment: str | None,
    source_url: str | None,
    dry_run: bool,
) -> str:
    """Build, preview and optionally submit the CLI or web job's approval report."""
    torrent_id, tracker = parse_target(target, tracker)
    choices = [code for code in trackers.tracker_list if code in ("RED", "OPS")]
    if not choices:
        raise click.ClickException("Configure RED or OPS before requesting approval.")
    if tracker is None:
        tracker = choices[0] if len(choices) == 1 else await trackers.choose_tracker(choices)
    if tracker not in choices:
        raise click.ClickException("Choose a RED or OPS tracker present in your config.")
    if tracker == "RED" and report_type is None:
        report_type = await interaction.prompt("Report type", type=click.Choice(["master", "web"]))
    if source_url is None:
        source_url = await interaction.prompt("Source URL (optional)", default="")
    if comment is None:
        comment = await generate_lossy_approval_comment(source_url, [], force_prompt_lossy_master=True)
    if not (comment or "").strip() and not (source_url or "").strip():
        raise click.BadParameter("Provide a comment or source URL explaining the source.")
    text = _add_spectral_links_to_lossy_comment(comment, source_url, None, None)
    label = "WEB" if tracker == "RED" and report_type == "web" else "Master"
    click.echo(f"{tracker} torrent {torrent_id}: Lossy {label} Approval")
    click.echo(text)
    if dry_run or not await interaction.confirm("Submit this approval report?", default=False):
        return text
    client = trackers.get_class(tracker)()
    try:
        await client.ensure_authenticated()
        await client.report_lossy_master(torrent_id, text, "WEB" if report_type == "web" else "CD")
    except UnknownOutcomeError as err:
        raise click.ClickException(
            f"The report may have been submitted. Check the torrent before trying again: {err}"
        ) from err
    except RequestError as err:
        raise click.ClickException(f"Could not submit the report: {err}") from err
    finally:
        await client.close()
    click.echo("Reported torrent for Lossy Master/WEB Approval Request.")
    return text
