"""Approval reports for torrents that are already on a tracker."""

from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

import asyncclick as click
import msgspec

from salmon import cfg, interaction, trackers
from salmon.common import commandgroup, get_audio_files
from salmon.common.strings import numeric_id
from salmon.errors import RequestError, UnknownOutcomeError
from salmon.tagger.audio_info import gather_audio_info
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import spectrals


async def verified_report_media(client: BaseGazelleApi, torrent_id: int) -> str:
    """Verify the explicitly selected FLAC is the authenticated user's upload and return its media (e.g. "WEB").

    Metadata similarity is not provenance. Raises RequestError when the torrent, its owner or its media cannot be
    verified.
    """
    # Reports use cookie authentication. Verify that account, not a possibly different API-key account.
    user_id = client.account_id
    if client.api_key:
        response = await client._request(
            "GET", client.base_url + "/ajax.php", params={"action": "index"}, prefer_api_key=False
        )
        try:
            account = msgspec.json.decode(response.text)
            cookie_authkey = account["response"]["authkey"]
            user_id = numeric_id(account["response"]["id"]) if account["status"] == "success" else None
        except (ValueError, KeyError, TypeError):
            raise RequestError("Cannot verify the authenticated reporting account. No reports sent.") from None
        if not cookie_authkey or cookie_authkey != client.authkey:
            raise RequestError("Cookie and API credentials could not be verified as the same account. No reports sent.")
    if user_id is None or user_id <= 0:
        raise RequestError("Cannot verify the authenticated reporting account. No reports sent.")
    found = await client.api_call("torrent", params={"id": torrent_id})
    target = found.get("torrent")
    group = found.get("group")
    if not isinstance(target, dict) or numeric_id(target.get("id")) != torrent_id:
        raise RequestError("Cannot identify the selected torrent unambiguously. No reports sent.")
    if target.get("format") != "FLAC":
        raise RequestError("Only an explicitly selected FLAC is eligible; lossy transcodes are not reported.")
    uploader = numeric_id(target.get("userId"))
    if uploader is None or uploader <= 0 or uploader != user_id:
        raise RequestError("Only verified uploads of the authenticated user can be reported. No reports sent.")
    group_id = numeric_id(group.get("id")) if isinstance(group, dict) else None
    media = target.get("media")
    if group_id is None or group_id <= 0 or not isinstance(media, str) or not media.strip():
        raise RequestError("Cannot verify the FLAC source metadata. No reports sent.")
    return media


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
@click.option("--tracker", type=click.Choice(["RED", "OPS"], case_sensitive=False))
@click.option("--comment", help="Source information and evidence for the approval request.")
@click.option("--source-url", help="Release source URL.")
@click.option("--folder", help="Album folder containing the selected torrent's audio.")
@click.option("--dry-run", "--print-only", is_flag=True, help="Print the report without contacting the tracker.")
async def report(
    target: str,
    tracker: str | None,
    comment: str | None,
    source_url: str | None,
    dry_run: bool,
    folder: str | None = None,
) -> None:
    """Request lossy approval for your verified FLAC upload.

    Temporary safeguard, not confirmed tracker rules: only your authenticated account's
    explicitly selected FLAC is eligible. Transcodes are not automatically reported.
    Unverifiable ownership or source metadata prevents submission.
    """
    await run_lossy_report(target, tracker, comment, source_url, dry_run, folder)


async def _spectral_evidence(folder: str, tracker: str) -> tuple[dict[int, str], dict[int, list[str]]]:
    with TemporaryDirectory(dir=cfg.directory.tmp_dir or None) as output:
        ids = await spectrals.generate_spectrals_all(folder, output, gather_audio_info(folder))
        await spectrals.compress_chosen_spectrals(output, ids)
        urls = await spectrals.upload_spectrals(output, ids, tracker, ask_host=False)
        if not urls or urls.keys() != ids.keys():
            raise ValueError("Missing spectral images.")
        return ids, urls


def _preview_folder(folder: str, torrent_id: int) -> None:
    if not folder or not Path(folder).is_dir():
        raise click.BadParameter("Album folder must be an existing directory.")
    files = get_audio_files(folder)
    if not files:
        raise click.BadParameter("Album folder contains no audio files.")
    click.echo(f"Album folder for torrent {torrent_id}: {folder} ({len(files)} audio files)")


async def run_lossy_report(
    target: str,
    tracker: str | None,
    comment: str | None,
    source_url: str | None,
    dry_run: bool,
    folder: str | None = None,
) -> str:
    """Build, preview and optionally submit the CLI or web job's approval report."""
    torrent_id, tracker = parse_target(target, tracker)
    if folder is not None:
        _preview_folder(folder, torrent_id)
    choices = [code for code in trackers.tracker_list if code in ("RED", "OPS")]
    if not choices:
        raise click.ClickException("Configure RED or OPS before requesting approval.")
    if tracker is None:
        tracker = choices[0] if len(choices) == 1 else await trackers.choose_tracker(choices)
    if tracker not in choices:
        raise click.ClickException("Choose a RED or OPS tracker present in your config.")
    if source_url is None:
        source_url = await interaction.prompt("Source URL (optional)", default="")
    if comment is None:
        prompt = "Comment about the source (your source URL and any spectral links will be included)"
        comment = await interaction.prompt(prompt, default="")
        while not comment and not source_url:
            click.echo("Provide a comment explaining the source.")
            comment = await interaction.prompt(prompt, default="")
    if not (comment or "").strip() and not (source_url or "").strip():
        raise click.BadParameter("Provide a comment or source URL explaining the source.")
    text = spectrals._add_spectral_links_to_lossy_comment(comment, source_url, None, None)
    click.echo(f"{tracker} torrent {torrent_id}")
    click.echo(text)
    if dry_run:
        if folder:
            click.echo("Print only: spectral images were neither generated nor uploaded.")
        click.echo("Print only: FLAC eligibility and authenticated ownership were not verified; nothing was submitted.")
        return text
    question = "Verify this FLAC belongs to your authenticated account?"
    if folder:
        question = "Verify this FLAC is yours and upload spectral images from the displayed folder if eligible?"
    if not await interaction.confirm(question, default=False):
        return text
    client = trackers.get_class(tracker)()
    try:
        await client.ensure_authenticated()
        media = await verified_report_media(client, torrent_id)
        if folder:
            try:
                ids, urls = await _spectral_evidence(folder, tracker)
                text = spectrals._add_spectral_links_to_lossy_comment(comment, source_url, urls, ids)
            except Exception as err:
                click.echo(f"Could not attach spectrals: {err}")
                if not await interaction.confirm("Continue without spectrals?", default=False):
                    raise click.ClickException("Report aborted before submission.") from err
            click.echo(text)
        if await interaction.confirm(
            f"Submit this approval report for {tracker} torrent {torrent_id} ({media})?", default=False
        ):
            await client.report_lossy_master(torrent_id, text, media)
            click.echo(f"Reported torrent {torrent_id}.")
    except UnknownOutcomeError as err:
        raise UnknownOutcomeError(
            f"The report may have been submitted. Check torrent {torrent_id} on {tracker} before reporting again."
        ) from err
    except RequestError as err:
        raise click.ClickException(f"Could not submit the report: {err}") from err
    finally:
        await client.close()
    return text
