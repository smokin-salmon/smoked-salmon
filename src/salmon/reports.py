"""Approval reports for torrents that are already on a tracker."""

from urllib.parse import parse_qs, urlsplit

import asyncclick as click

from salmon import interaction, trackers
from salmon.common import commandgroup
from salmon.common.strings import numeric_id
from salmon.errors import RequestError, UnknownOutcomeError
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader.dupe_checker import _site_edition
from salmon.uploader.spectrals import _add_spectral_links_to_lossy_comment, generate_lossy_approval_comment

MAX_REPORTS = 20


async def related_report_targets(client: BaseGazelleApi, torrent_id: int) -> list[int]:
    """Two lookups find the target uploader's same-edition/media torrents, without per-torrent requests."""
    found = await client.api_call("torrent", params={"id": torrent_id})
    target = found.get("torrent")
    info = found.get("group")
    if not isinstance(target, dict) or not isinstance(info, dict):
        raise RequestError("The tracker did not return the torrent and its group. No reports sent.")
    group_id = numeric_id(info.get("id"))
    uploader = numeric_id(target.get("userId"))
    media = target.get("media")
    if (
        not group_id
        or group_id <= 0
        or not uploader
        or uploader <= 0
        or not isinstance(media, str)
        or not media.strip()
    ):
        raise RequestError("Cannot identify the torrent's group, uploader or media. No reports sent.")
    group = await client.torrentgroup(group_id)
    rows = group.get("torrents")
    group_info = group.get("group")
    if not isinstance(rows, list) or not isinstance(group_info, dict) or numeric_id(group_info.get("id")) != group_id:
        raise RequestError("The tracker did not return the expected torrent group. No reports sent.")
    edition = _site_edition(target, group)
    matches: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RequestError("The tracker returned an invalid torrent entry. No reports sent.")
        candidate = numeric_id(row.get("id"))
        if (
            candidate is not None
            and candidate > 0
            and numeric_id(row.get("userId")) == uploader
            and row.get("media") == media
            and _site_edition(row, group) == edition
        ):
            matches.add(candidate)
    if torrent_id not in matches:
        raise RequestError("The target is missing or changed in its torrent group. No reports sent.")
    if len(matches) > MAX_REPORTS:
        raise RequestError(
            f"Found {len(matches)} matching torrents; the limit is {MAX_REPORTS}. Report manually instead."
        )
    return [torrent_id, *sorted(matches - {torrent_id})]


async def submit_reports(client: BaseGazelleApi, targets: list[int], text: str, source: str) -> None:
    """Send the confirmed batch sequentially, stopping on the first failure without replaying any report."""
    completed: list[int] = []
    for index, torrent_id in enumerate(targets):
        try:
            await client.report_lossy_master(torrent_id, text, source)
        except RequestError as err:
            already = ", ".join(map(str, completed)) or "none"
            remaining = ", ".join(map(str, targets[index + 1 :])) or "none"
            reason = (
                f"The report for torrent {torrent_id} may have been submitted. Check it before trying again."
                if isinstance(err, UnknownOutcomeError)
                else f"Could not submit the report for torrent {torrent_id}."
            )
            raise click.ClickException(
                f"{reason}\nAlready reported: {already}\nNot attempted: {remaining}\n"
                f"Do not rerun the batch; check these torrents and report any remaining ones manually.\n{err}"
            ) from err
        completed.append(torrent_id)
        click.echo(f"Reported torrent {torrent_id} for Lossy Master/WEB Approval Request.")


def parse_target(target: str, tracker: str | None) -> tuple[int, str | None]:
    """Accept a positive torrent ID or a recognized torrent permalink."""
    value = target.strip()
    if "://" in value:
        try:
            url = urlsplit(value)
            code = trackers.tracker_url_code_map.get(url.netloc.lower())
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


@commandgroup.command()
@click.argument("target")
@click.option("--type", "report_type", type=click.Choice(["master", "web"]))
@click.option("--tracker", type=click.Choice(["RED", "OPS"], case_sensitive=False))
@click.option("--comment", help="Source information and evidence for the approval request.")
@click.option("--source-url", help="Release source URL.")
@click.option("--dry-run", "--print-only", is_flag=True, help="Print the report without contacting the tracker.")
async def report(
    target: str,
    report_type: str | None,
    tracker: str | None,
    comment: str | None,
    source_url: str | None,
    dry_run: bool,
) -> None:
    """Request lossy approval for a torrent and its uploader's same-edition transcodes. No local audio is needed."""
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
    if dry_run:
        click.echo("Print only: related torrents were not looked up. Report matching transcodes manually too.")
        return text
    if not await interaction.confirm("Look up this uploader's same-edition torrents for approval?", default=False):
        return text
    client = trackers.get_class(tracker)()
    try:
        await client.ensure_authenticated()
        targets = await related_report_targets(client, torrent_id)
        click.echo(f"Torrents to report on {tracker}: {', '.join(map(str, targets))}")
        if await interaction.confirm(f"Submit this approval report for all {len(targets)} torrents?", default=False):
            await submit_reports(client, targets, text, "WEB" if report_type == "web" else "CD")
    except RequestError as err:
        raise click.ClickException(f"Could not submit the report: {err}") from err
    finally:
        await client.close()
    return text
