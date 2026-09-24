from collections.abc import Callable, Sequence
from typing import Any

import anyio
import asyncclick as click
import pyperclip

from salmon import cfg
from salmon.common import AliasedCommands, commandgroup
from salmon.config.image_hosts import spectrals_refusal
from salmon.errors import ImageUploadFailed
from salmon.images import catbox, imgbb, imgbox, oeimg, ptscreens, ra, red
from salmon.images.base import BaseImageUploader
from salmon.trackers.red import RedApi

HOSTS = {
    "catbox": catbox,
    "ptscreens": ptscreens,
    "oeimg": oeimg,
    "imgbb": imgbb,
    "imgbox": imgbox,
    "ra": ra,
    "red": red,
}

# How many uploads to one image host a batch runs at once, each on a connection of its own,
# reused from one image to the next. Chosen by measurement against a local fake host (#475).
UPLOAD_CONNECTIONS = 8


def validate_image_host(ctx: click.Context, param: click.Parameter, value: str) -> Any:
    """Validate and return the image host module.

    Args:
        ctx: Click context.
        param: Click parameter.
        value: The image host name.

    Returns:
        The image host module.

    Raises:
        click.BadParameter: If the image host is invalid.
    """
    try:
        return HOSTS[value]
    except KeyError:
        raise click.BadParameter(f"{value} is not a valid image host") from None


@commandgroup.group(cls=AliasedCommands)
async def images() -> None:
    """Create and manage uploads to image hosts."""
    pass


@images.command()
@click.argument(
    "filepaths",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    nargs=-1,
)
@click.option(
    "--image-host",
    "-i",
    help="The name of the image host to upload to",
    default=cfg.image.image_uploader,
    callback=validate_image_host,
)
async def up(filepaths: tuple[str, ...], image_host: Any) -> None:
    """Upload images to an image host."""
    await upload_images(filepaths, image_host)


async def upload_images(filepaths: tuple, image_host) -> list[str]:
    """Upload images to the specified host, over at most UPLOAD_CONNECTIONS connections.

    Args:
        filepaths: Tuple of file paths to upload.
        image_host: The image host module.

    Returns:
        List of uploaded URLs.
    """
    failures: list[Exception] = []
    try:
        results = await _upload_groups(
            image_host.ImageUploader(),
            [[filepath] for filepath in filepaths],
            on_failure=lambda _index, error: failures.append(error),
        )
    except ValueError as error:
        click.secho(f"Image Upload Failed. {error}", fg="red")
        raise ImageUploadFailed("Failed to upload image") from error
    if failures:
        click.secho(f"Image Upload Failed. {failures[0]}", fg="red")
        raise ImageUploadFailed("Failed to upload image") from failures[0]

    urls = [group[0] for group in results if group is not None]
    for url in urls:
        click.secho(url)
    if cfg.upload.description.copy_uploaded_url_to_clipboard:
        pyperclip.copy("\n".join(urls))
    return urls


async def _upload_groups(
    uploader: BaseImageUploader,
    groups: Sequence[Sequence[str]],
    on_start: Callable[[int], None] = lambda _index: None,
    on_failure: Callable[[int, ImageUploadFailed], None] = lambda _index, _error: None,
) -> list[list[str] | None]:
    """Upload groups of images to one host, over at most UPLOAD_CONNECTIONS connections.

    Images queue for a free connection, in order, and a slow or failed upload holds up none
    of the others. Once an upload fails, no new group starts: the host may be down, so the
    groups not started yet are left for the caller to send elsewhere. The images of a group
    already started still go.

    Args:
        uploader: The image uploader to send every image through.
        groups: The image paths to upload, in groups that succeed or fail together.
        on_start: Called with a group's index when its first image starts uploading.
        on_failure: Called with a group's index and the error when one of its images fails.

    Returns:
        Each group's URLs, in the order of its paths, or None if the group failed or never started.
    """
    queue = iter([(index, position, path) for index, paths in enumerate(groups) for position, path in enumerate(paths)])
    urls: list[list[str]] = [[""] * len(paths) for paths in groups]
    started: set[int] = set()
    failed: set[int] = set()

    async def worker() -> None:
        # Every worker takes from the same iterator, so each image is taken exactly once.
        for index, position, path in queue:
            if index not in started:
                if failed:
                    return  # The rest of the queue belongs to groups not started either.
                started.add(index)
                on_start(index)
            try:
                urls[index][position], _ = await uploader.upload_file(path)
            except ImageUploadFailed as error:
                if index not in failed:
                    failed.add(index)
                    on_failure(index, error)

    raised: BaseException | None = None
    try:
        # The pool closes only once every worker is done, on success, failure or cancellation.
        async with uploader.connections(UPLOAD_CONNECTIONS), anyio.create_task_group() as tg:
            for _ in range(UPLOAD_CONNECTIONS):
                tg.start_soon(worker)
    except BaseExceptionGroup as group:
        # Raise what an upload raised as it is, as a plain gather would, not wrapped in a group.
        if len(group.exceptions) != 1:
            raise
        raised = group.exceptions[0]
    if raised is not None:
        raise raised

    return [group_urls if index in started and index not in failed else None for index, group_urls in enumerate(urls)]


async def upload_cover(cover_path: str | None, host: str | None = None, red_api: RedApi | None = None) -> str | None:
    """Upload cover image to an image host.

    Args:
        cover_path: Path to the cover image file.
        host: The image host to upload to. Defaults to the configured cover_uploader.
        red_api: The RED client that RED's image host uploads through. Without one, it makes its own.

    Returns:
        The uploaded image URL, or None if upload failed.
    """
    if not cover_path:
        click.secho("\nNo Cover Image Path was provided to upload...", fg="red", nl=False)
        return None
    host = host or cfg.image.cover_uploader
    click.secho(f"Uploading cover to {host}...", fg="yellow", nl=False)
    try:
        uploader = red.ImageUploader(red_api) if host == "red" else HOSTS[host].ImageUploader()
        url, _ = await uploader.upload_file(cover_path)
        click.secho(f" done! {url}", fg="yellow")
        return url
    except (ImageUploadFailed, ValueError) as error:
        click.secho(f" failed :( {error}", fg="red")
        return None


async def upload_spectrals(spectrals, uploader=None, successful=None) -> dict:
    """Upload spectral images to image host.

    Args:
        spectrals: List of (spec_id, filename, spectral_paths) tuples.
        uploader: The image host module to use.
        successful: Set of already successful spec_ids.

    Returns:
        Dictionary mapping spec_id to list of URLs.
    """
    if uploader is None:
        uploader = HOSTS[cfg.image.specs_uploader]

    successful = successful or set()
    pending = [(sid, filename, paths) for sid, filename, paths in spectrals if sid not in successful]

    def on_start(index: int) -> None:
        click.secho(f"Uploading spectrals for {pending[index][1]}...", fg="yellow")

    def on_failure(index: int, error: ImageUploadFailed) -> None:
        click.secho(f"Failed to upload spectrals for {pending[index][1]}: {error}", fg="red")

    results = await _upload_groups(uploader.ImageUploader(), [paths for _, _, paths in pending], on_start, on_failure)

    response = {}
    for (sid, _, _), urls in zip(pending, results, strict=True):
        if urls is not None:
            response[sid] = urls
            successful.add(sid)
    if len(response) < len(pending):
        retry_result = await _handle_failed_spectrals(spectrals, successful)
        return {**response, **retry_result}
    return response


async def _handle_failed_spectrals(spectrals, successful) -> dict:
    """Handle failed spectral uploads by prompting for a new host.

    Args:
        spectrals: List of spectral tuples.
        successful: Set of already successful spec_ids.

    Returns:
        Dictionary of uploaded URLs.
    """
    while True:
        # Recomputed every iteration (not cached at import time) so it always reflects the
        # rules in salmon.config.image_hosts, the single source shared with config validation
        # for specs_uploader.
        forbidden = {host: reason for host in HOSTS if (reason := spectrals_refusal(host)) is not None}
        allowed_hosts = [host for host in HOSTS if host not in forbidden]
        host_input: str = await click.prompt(
            click.style(
                "Some spectrals failed to upload. Which image host would you like to retry "
                f"with? (Options: {', '.join(allowed_hosts)})",
                fg="magenta",
                bold=True,
            ),
            default=cfg.image.specs_uploader,
        )
        host = host_input.lower()
        if host in forbidden:
            click.secho(f"{host} can't be used for spectrals: {forbidden[host]}.", fg="red")
        elif host not in HOSTS:
            click.secho(f"{host} is an invalid image host. Please choose another one.", fg="red")
        else:
            return await upload_spectrals(spectrals, uploader=HOSTS[host], successful=successful)
