import io
import os
import re

import aiohttp
import anyio
import asyncclick as click
import humanfriendly
from mutagen import PaddingInfo
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

from salmon import cfg
from salmon.common import get_audio_files


def get_cover_from_path(path):
    """
    Search a folder for a cover image, return its path.
    """
    for filename in os.listdir(path):
        if re.match(r"^(cover|folder)\.(jpe?g|png)$", filename, flags=re.IGNORECASE):
            fpath = os.path.join(path, filename)
            return fpath
    click.secho(f"Did not find a cover in path {path}", fg="red")
    return None


async def download_cover_if_nonexistent(path: str, cover_url: str | None) -> tuple[str | None, bool | None]:
    """Download cover if not already present in folder.

    Args:
        path: Source folder path.
        cover_url: URL for cover image to download.

    Returns:
        Tuple of (cover_path, was_downloaded). Both None if failed.
    """
    # use local file if matches filter
    cover_path = get_cover_from_path(path)
    if cover_path:
        click.secho(f"\nUsing existing cover image found: {cover_path}...", fg="yellow")
        return cover_path, False
    # use url provided
    if cover_url:
        click.secho("\nDownloading Cover Image...", fg="yellow")
        cover_path = await _download_cover(path, cover_url)
        if cover_path:
            return cover_path, True
    click.secho("\nNo existing Cover Image found in Source Folder, no Cover Image downloaded", fg="red")
    return None, None


def _is_valid_cover(cover_path: str) -> bool:
    """Check if the file at cover_path is a valid JPEG or PNG image.

    Args:
        cover_path: Path to the image file.

    Returns:
        True if the file is a valid JPEG or PNG image.
    """
    try:
        mime = Image.open(cover_path).get_format_mimetype()
    except Exception:
        return False
    return mime in ("image/jpeg", "image/png")


async def _download_cover(path: str, cover_url: str) -> str | None:
    """Download cover image from URL.

    Args:
        path: Directory to save the cover.
        cover_url: URL to download from.

    Returns:
        Path to downloaded cover or None on failure.
    """
    ext = os.path.splitext(cover_url)[1]
    c = "c" if cfg.upload.formatting.lowercase_cover else "C"
    headers = {"User-Agent": "smoked-salmon-v1"}
    cover_image_filename = c + "over" + ext
    cover_path = os.path.join(path, cover_image_filename)

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(cover_url, headers=headers) as response,
        ):
            if response.status >= 400:
                click.secho(f"\nFailed to download cover image (ERROR {response.status})", fg="red")
                return None

            async with await anyio.open_file(cover_path, "wb") as f:
                async for chunk in response.content.iter_chunked(5096):
                    await f.write(chunk)
    except aiohttp.ClientError as e:
        click.secho(f"\nFailed to download cover image (ERROR {e})", fg="red")
        return None

    if not _is_valid_cover(cover_path):
        os.remove(cover_path)
        click.secho("\nFailed to download cover image (ERROR file is not an image [JPEG, PNG])", fg="red")
        return None

    click.secho(f"Cover image downloaded: {cover_image_filename} ", fg="yellow")
    return cover_path


def compress_to_target_size(image, target_size):
    quality = 95

    buffer = io.BytesIO()

    while True:
        image.save(buffer, "jpeg", optimize=True, quality=quality)

        file_size = len(buffer.getvalue())

        if file_size <= target_size:
            print(f"Successfully compressed to {humanfriendly.format_size(file_size, binary=True)}")
            return buffer.getvalue()

        quality -= 5

        if quality <= 75:
            print("Quality too low, cannot compress further!")
            break


def get_8kib_padding(info: PaddingInfo):
    return humanfriendly.parse_size("8KiB")


def compress_pictures(path):
    for filename in get_audio_files(path):
        click.secho(f"Processing file: {filename}", fg="blue")
        audio = FLAC(os.path.join(path, filename))

        padding_size = sum(block.length for block in audio.metadata_blocks if block.code == 1)

        cover_sizes = sum(len(picture.data) for picture in audio.pictures)
        click.secho(
            (
                f"Padding size: {humanfriendly.format_size(padding_size, binary=True)}, "
                f"Cover size: {humanfriendly.format_size(cover_sizes, binary=True)}"
            ),
            fg="cyan",
        )

        cover_file = get_cover_from_path(path)

        if padding_size + cover_sizes > humanfriendly.parse_size("1MiB"):
            click.secho(
                f"Total size ({humanfriendly.format_size(padding_size + cover_sizes, binary=True)}) exceeds 1MiB!",
                fg="yellow",
            )

            for picture in audio.pictures:
                if picture.type == PictureType.COVER_FRONT:
                    if picture.mime == "image/jpeg":
                        extension = "jpg"
                    elif picture.mime == "image/png":
                        extension = "png"
                    else:
                        extension = "jpg"  # Default fallback

                    if not cover_file:
                        cover_file = os.path.join(path, f"cover.{extension}")
                        with open(cover_file, "wb") as img:
                            img.write(picture.data)
                        click.secho(f"Extracted cover to: {cover_file}", fg="green")

            audio.clear_pictures()
            audio.save(padding=get_8kib_padding)

        if audio.pictures == []:
            click.secho("Attempting to add external cover...", fg="magenta")
            if not cover_file:
                click.secho("No cover file found!", fg="red")
                continue

            with open(cover_file, "rb") as c:
                data = c.read()

            max_embedded_image_size = humanfriendly.parse_size("1MiB") - humanfriendly.parse_size("8KiB")

            picture = Picture()

            if len(data) < max_embedded_image_size:
                click.secho(
                    f"Cover size ({humanfriendly.format_size(len(data), binary=True)}) within limit",
                    fg="bright_green",
                )
                picture.mime = Image.open(cover_file).get_format_mimetype()
            else:
                click.secho(
                    f"Resizing oversized cover ({humanfriendly.format_size(len(data), binary=True)})...",
                    fg="yellow",
                )
                image = Image.open(cover_file)
                image.thumbnail((1000, 1000))
                data = compress_to_target_size(image, max_embedded_image_size)
                picture.mime = "image/jpeg"

            picture.data = data
            picture.type = PictureType.COVER_FRONT
            audio.add_picture(picture)
            audio.save(padding=get_8kib_padding)
            click.secho(f"Saved {filename} with optimized cover", fg="bright_green")
        else:
            click.secho("Existing covers meet size requirements", fg="bright_white")
