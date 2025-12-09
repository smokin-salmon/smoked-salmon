import asyncio
import sqlite3

import click
import pyperclip

from salmon import cfg
from salmon.common import AliasedCommands, commandgroup, run_gather
from salmon.database import DB_PATH
from salmon.errors import ImageUploadFailed
from salmon.images import catbox, emp, imgbb, imgbox, oeimg, ptpimg, ptscreens

HOSTS = {
    "ptpimg": ptpimg,
    "emp": emp,
    "catbox": catbox,
    "ptscreens": ptscreens,
    "oeimg": oeimg,
    "imgbb": imgbb,
    "imgbox": imgbox,
}


def validate_image_host(ctx, param, value):
    """Validate and return the image host module."""
    try:
        return HOSTS[value]
    except KeyError:
        raise click.BadParameter(f"{value} is not a valid image host") from None


@commandgroup.group(cls=AliasedCommands)
def images():
    """Create and manage uploads to image hosts"""
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
def up(filepaths, image_host):
    """Upload images to an image host"""
    asyncio.run(upload_images(filepaths, image_host))


async def upload_images(filepaths: tuple, image_host) -> list[str]:
    """Upload images to the specified host asynchronously.

    Args:
        filepaths: Tuple of file paths to upload.
        image_host: The image host module.

    Returns:
        List of uploaded URLs.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        urls = []
        uploader = image_host.ImageUploader()
        try:
            tasks = [uploader.upload_file(f) for f in filepaths]
            for url, deletion_url in await asyncio.gather(*tasks):
                cursor.execute(
                    "INSERT INTO image_uploads (url, deletion_url) VALUES (?, ?)",
                    (url, deletion_url),
                )
                click.secho(url)
                urls.append(url)
            conn.commit()
            if cfg.upload.description.copy_uploaded_url_to_clipboard:
                pyperclip.copy("\n".join(urls))
            return urls
        except (ImageUploadFailed, ValueError) as error:
            click.secho(f"Image Upload Failed. {error}", fg="red")
            raise ImageUploadFailed("Failed to upload image") from error


@images.command()
@click.option("--limit", "-l", type=click.INT, default=20, help="The number of images to show")
@click.option(
    "--offset",
    "-o",
    type=click.INT,
    default=0,
    help="The number of images to offset by",
)
def ls(limit, offset):
    """View previously uploaded images"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, url, deletion_url, time FROM image_uploads ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        for row in cursor.fetchall():
            click.secho("")
            click.secho(f"{row['id']:04d}. ", fg="yellow", nl=False)
            click.secho(f"{row['time']} ", fg="green", nl=False)
            click.secho(f"{row['url']} ", fg="cyan", nl=False)
            if row["deletion_url"]:
                click.secho(f"Delete: {row['deletion_url']}", fg="red")


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
        uploader = HOSTS[cfg.image.cover_uploader].ImageUploader()
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
    while True:
        host = click.prompt(
            click.style(
                "Some spectrals failed to upload. Which image host would you like to retry "
                f"with? (Options: {', '.join(HOSTS.keys())})",
                fg="magenta",
                bold=True,
            ),
            default="ptpimg",
        ).lower()
        if host not in HOSTS:
            click.secho(f"{host} is an invalid image host. Please choose another one.", fg="red")
        else:
            return await upload_spectrals(spectrals, uploader=HOSTS[host], successful=successful)


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


async def _run_uploads(upload_function, filepaths):
    loop = asyncio.get_running_loop()
    tasks = [loop.run_in_executor(None, lambda f=f: upload_function(f)) for f in filepaths]
    return await asyncio.gather(*tasks)


async def _run_cover_upload(cover_path):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda f=cover_path: HOSTS[cfg.image.cover_uploader].ImageUploader().upload_file(f)[0],
    )
