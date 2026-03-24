import asyncio
from typing import Any

import asyncclick as click
import pyperclip

from salmon import cfg
from salmon.common import AliasedCommands, commandgroup
from salmon.errors import ImageUploadFailed

_HOSTS: dict[str, Any] | None = None


def get_hosts() -> dict[str, Any]:
    """Load image-host integrations only when an upload flow needs them."""
    global _HOSTS
    if _HOSTS is None:
        from salmon.images import catbox, imgbb, imgbox, oeimg, ptpimg, ptscreens

        _HOSTS = {
            "ptpimg": ptpimg,
            "catbox": catbox,
            "ptscreens": ptscreens,
            "oeimg": oeimg,
            "imgbb": imgbb,
            "imgbox": imgbox,
        }
    return _HOSTS


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
    hosts = get_hosts()
    try:
        return hosts[value]
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
    """Upload images to the specified host asynchronously.

    Args:
        filepaths: Tuple of file paths to upload.
        image_host: The image host module.

    Returns:
        List of uploaded URLs.
    """
    urls = []
    uploader = image_host.ImageUploader()
    try:
        tasks = [uploader.upload_file(f) for f in filepaths]
        for url, _deletion_url in await asyncio.gather(*tasks):
            click.secho(url)
            urls.append(url)
        if cfg.upload.description.copy_uploaded_url_to_clipboard:
            pyperclip.copy("\n".join(urls))
        return urls
    except (ImageUploadFailed, ValueError) as error:
        click.secho(f"Image Upload Failed. {error}", fg="red")
        raise ImageUploadFailed("Failed to upload image") from error


def chunker(seq, size=4):
    for pos in range(0, len(seq), size):
        yield seq[pos : pos + size]


async def upload_cover(cover_path: str | None) -> str | None:
    """Upload cover image to the configured image host.

    Args:
        cover_path: Path to the cover image file.

    Returns:
        The uploaded image URL, or None if upload failed.
    """
    if not cover_path:
        click.secho("\nNo Cover Image Path was provided to upload...", fg="red", nl=False)
        return None
    click.secho(f"Uploading cover to {cfg.image.cover_uploader}...", fg="yellow", nl=False)
    try:
        uploader = get_hosts()[cfg.image.cover_uploader].ImageUploader()
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
        uploader = get_hosts()[cfg.image.specs_uploader]

    response = {}
    successful = successful or set()
    one_failed = False
    uploader_instance = uploader.ImageUploader()

    for specs_block in chunker(spectrals):
        tasks = [
            _spectrals_handler(sid, filename, sp, uploader_instance)
            for sid, filename, sp in specs_block
            if sid not in successful
        ]
        for sid, urls in await asyncio.gather(*tasks):
            if urls:
                response[sid] = urls
                successful.add(sid)
            else:
                one_failed = True
        if one_failed:
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
    hosts = get_hosts()
    while True:
        host_input: str = await click.prompt(
            click.style(
                "Some spectrals failed to upload. Which image host would you like to retry "
                f"with? (Options: {', '.join(hosts.keys())})",
                fg="magenta",
                bold=True,
            ),
            default="ptpimg",
        )
        host = host_input.lower()
        if host not in hosts:
            click.secho(f"{host} is an invalid image host. Please choose another one.", fg="red")
        else:
            return await upload_spectrals(spectrals, uploader=hosts[host], successful=successful)


async def _spectrals_handler(spec_id, filename, spectral_paths, uploader_instance):
    """Handle uploading spectrals for a single file.

    Args:
        spec_id: The spectral ID.
        filename: The audio filename.
        spectral_paths: List of spectral image paths.
        uploader_instance: The image uploader instance.

    Returns:
        Tuple of (spec_id, list of URLs or None).
    """
    try:
        click.secho(f"Uploading spectrals for {filename}...", fg="yellow")
        tasks = [uploader_instance.upload_file(f) for f in spectral_paths]
        results = await asyncio.gather(*tasks)
        return spec_id, [url for url, _ in results]
    except ImageUploadFailed as e:
        click.secho(f"Failed to upload spectrals for {filename}: {e}", fg="red")
        return spec_id, None
