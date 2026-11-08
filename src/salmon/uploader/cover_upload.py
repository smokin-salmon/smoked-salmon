"""Finding a release's cover and uploading it to an image host the tracker can display."""

import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

import asyncclick as click

from salmon import cfg, dryrun, interaction
from salmon.config.image_hosts import cover_refusal
from salmon.images import HOSTS, upload_cover
from salmon.tagger.cover import download_cover_if_nonexistent
from salmon.trackers.red import RedApi

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


async def get_cover_url(
    tracker: str,
    cover_urls: dict[str, str | None],
    path: str,
    cover_source: str | None,
    remove_downloaded: bool,
    red_api: RedApi | None = None,
    host: str | None = None,
) -> tuple[str | None, bool]:
    """Get the cover URL for a new group on a tracker, uploading the cover if needed.

    Each tracker can have its own cover host, so a cover uploaded for one tracker is
    only reused by trackers that share its host. A failed upload is retried next time.

    Args:
        tracker: The tracker site code, e.g. "RED".
        cover_urls: Cover URLs already uploaded in this run, by image host. Updated in place.
        path: The release folder.
        cover_source: URL to download the cover from if the folder has none.
        remove_downloaded: Delete the cover file after uploading, if it was downloaded.
        red_api: The RED client that RED's image host uploads through.
        host: Upload to this host instead of the tracker's configured one, for a retry on
            another host. The URL is still cached in cover_urls under this host.

    Returns:
        The cover URL (None if none is cached and the upload failed or no cover was found), and
        whether a cover file was found: False means there is nothing to retry uploading, True
        with a None URL means the upload itself failed. A dry run uploads nothing: the URL is
        what stands in for it.
    """
    host = host or cfg.image.host_for(tracker, "cover_uploader")
    if not cover_urls.get(host):
        cover_path, is_downloaded = await download_cover_if_nonexistent(path, cover_source)
        if dryrun.active() and cover_path:
            dryrun.say(f"not uploading the cover {os.path.basename(cover_path)} to {host}.")
            cover_urls[host] = dryrun.image_url(cover_path, host)
        else:
            cover_urls[host] = await upload_cover(cover_path, host, red_api)
        if is_downloaded and remove_downloaded and cover_path:
            click.secho("Removing downloaded Cover Image File", fg="yellow")
            os.remove(cover_path)
        return cover_urls[host], cover_path is not None
    return cover_urls[host], True


@asynccontextmanager
async def red_api_for_covers(gazelle_site: "BaseGazelleApi", host: str | None = None) -> AsyncIterator[RedApi | None]:
    """Get the RED client that a cover upload to `host` for gazelle_site's tracker goes through, if any.

    RED's image host authenticates with the RED API key, whichever tracker the cover is for. An
    upload to RED uses its own client. An upload to another tracker gets one RED client of its
    own, closed once the upload it was made for is done.

    Args:
        gazelle_site: The tracker API instance the upload is to.
        host: The image host the cover is going to. Defaults to the tracker's configured cover host.

    Yields:
        The RED client, or None if the cover is not going to RED's image host.
    """
    if isinstance(gazelle_site, RedApi):
        yield gazelle_site
    elif (host if host is not None else cfg.image.host_for(gazelle_site.site_code, "cover_uploader")) == "red":
        red_api = RedApi()
        try:
            yield red_api
        finally:
            await red_api.close()
    else:
        yield None


async def _choose_cover_host(tracker: str, default_host: str) -> str:
    """Ask which image host to retry a failed cover upload with.

    Args:
        tracker: The tracker site code the cover is for, e.g. "RED".
        default_host: The host offered as the default answer.

    Returns:
        The chosen host, valid as a cover host for tracker.
    """
    while True:
        forbidden = {host: reason for host in HOSTS if (reason := cover_refusal(host, tracker)) is not None}
        allowed_hosts = [host for host in HOSTS if host not in forbidden]
        host_input: str = await interaction.prompt(
            click.style(
                "Which image host would you like to retry the cover upload with? "
                f"(Options: {', '.join(allowed_hosts)})",
                fg="magenta",
                bold=True,
            ),
            default=default_host,
        )
        host = host_input.strip().lower()
        if host in forbidden:
            click.secho(f"{host} can't be used as a cover host for {tracker}: {forbidden[host]}.", fg="red")
        elif host not in HOSTS:
            click.secho(f"{host} is an invalid image host. Please choose another one.", fg="red")
        else:
            return host


async def resolve_cover_url(
    gazelle_site: "BaseGazelleApi",
    group_id: int | None,
    cover_urls: dict[str, str | None],
    path: str,
    cover_source: str | None,
    remove_downloaded: bool,
    given_url: str | None = None,
) -> tuple[bool, str | None]:
    """Get the cover URL to upload to a tracker with, asking before a new group goes up without one.

    An existing group already has its cover, so it needs none. A new group takes given_url, a cover uploaded
    already, when there is one: nothing is uploaded. For a new group with no cover,
    -yyy stops the upload; otherwise the user can go on without one, retry, or stop. A retry
    after a failed upload (a cover file was found) asks which host to retry with; a retry with no
    cover found just looks at the folder again.

    Args:
        gazelle_site: The tracker API instance the upload is to.
        group_id: The existing group to upload to, or None for a new group.
        cover_urls: Cover URLs already uploaded in this run, by image host. Updated in place.
        path: The release folder.
        cover_source: URL to download the cover from if the folder has none.
        remove_downloaded: Delete the cover file after uploading, if it was downloaded.
        given_url: The cover's URL, uploaded already (--cover-url), checked against the run's trackers.

    Returns:
        Whether to upload to this tracker, and the cover URL to upload with (None for none).
    """
    tracker = gazelle_site.site_code
    if group_id or given_url:
        # The folder gets its cover file as it would with an upload, which keeps a downloaded one.
        if not remove_downloaded:
            await download_cover_if_nonexistent(path, cover_source)
        return True, None if group_id else given_url

    default_host = cfg.image.host_for(tracker, "cover_uploader")
    host = default_host
    async with AsyncExitStack() as stack:
        red_api: RedApi | None = None
        while True:
            if host == "red" and red_api is None:
                red_api = await stack.enter_async_context(red_api_for_covers(gazelle_site, host))
            cover_url, cover_found = await get_cover_url(
                tracker, cover_urls, path, cover_source, remove_downloaded, red_api if host == "red" else None, host
            )
            if cover_url:
                return True, cover_url

            click.secho(
                f"\nNo cover image for this new group on {tracker}: none was found, its download failed, or the upload "
                f"to {host} failed.",
                fg="yellow",
                bold=True,
            )
            if await interaction.assume_defaults():
                click.secho(
                    "Not uploading a new group without a cover image with -yyy (upload.yes_all).", fg="red", bold=True
                )
                return False, None

            choice = await interaction.prompt(
                click.style("Continue without a cover image? [y/N/r]", fg="magenta"),
                default="n",
                show_default=False,
            )
            choice = choice.strip().lower()
            if choice in ("r", "retry"):
                if cover_found:
                    host = await _choose_cover_host(tracker, default_host)
                else:
                    click.secho("Looking for a cover image again...", fg="cyan")
            elif choice in ("y", "yes"):
                return True, None
            else:
                return False, None
