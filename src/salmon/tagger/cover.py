import contextlib
import io
import os
import re
import uuid

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

# RED allows at most 1 MiB of embedded pictures plus padding per file.
MAX_PICTURES_AND_PADDING = humanfriendly.parse_size("1MiB")


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
    # fall back to an embedded FLAC front cover
    embedded = _find_embedded_front_cover(path)
    if embedded:
        extension, data = embedded
        cover_path = os.path.join(path, f"cover.{extension}")
        _write_whole_file(cover_path, data)
        click.secho(f"Extracted embedded cover to: {cover_path}", fg="yellow")
        return cover_path, True
    click.secho("\nNo existing Cover Image found in Source Folder, no Cover Image downloaded", fg="red")
    return None, None


def _find_embedded_front_cover(path: str) -> tuple[str, bytes] | None:
    """Find the first FLAC (by `get_audio_files` order) with an embedded front cover.

    Only the first FLAC that has a front cover picture is considered; if that picture cannot be
    read as an image, no fallback to another FLAC is attempted.

    Args:
        path: The release folder.

    Returns:
        The extension and bytes to save the cover as a file, or None if no FLAC has a usable one.
    """
    for filename in get_audio_files(path):
        if not filename.lower().endswith(".flac"):
            continue
        audio = FLAC(os.path.join(path, filename))
        for picture in audio.pictures:
            if picture.type == PictureType.COVER_FRONT:
                return _as_cover_file(picture)
    return None


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
        # Each attempt replaces the last one, or the sizes add up and no quality ever fits.
        buffer.seek(0)
        buffer.truncate()
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


def pictures_and_padding_size(audio: FLAC) -> int:
    """Get the bytes a FLAC spends on embedded pictures and padding, which RED's 1 MiB limit counts."""
    padding_size = sum(block.length for block in audio.metadata_blocks if block.code == 1)
    return padding_size + sum(len(picture.write()) for picture in audio.pictures)


def find_oversized_pictures(path: str) -> dict[str, int]:
    """Find the FLACs in a folder whose embedded pictures plus padding exceed RED's 1 MiB limit.

    Args:
        path: The release folder.

    Returns:
        The size of the pictures plus padding, by file path relative to the folder.
    """
    oversized = {}
    for filename in get_audio_files(path):
        if not filename.lower().endswith(".flac"):
            continue
        size = pictures_and_padding_size(FLAC(os.path.join(path, filename)))
        if size > MAX_PICTURES_AND_PADDING:
            oversized[filename] = size
    return oversized


def _as_cover_file(picture: Picture) -> tuple[str, bytes] | None:
    """Get the extension and bytes to save an embedded picture as a cover file, which must be JPEG or PNG.

    JPEG and PNG are kept as they are, whatever MIME type the picture claims; other formats are converted to PNG.

    Returns:
        The extension and bytes, or None if the picture is not an image PIL can read.
    """
    try:
        with Image.open(io.BytesIO(picture.data)) as image:
            if image.format in ("JPEG", "PNG"):
                return ("jpg" if image.format == "JPEG" else "png"), picture.data
            buffer = io.BytesIO()
            image.convert("RGBA").save(buffer, "png")
            return "png", buffer.getvalue()
    except Exception:
        return None


def _write_whole_file(dest: str, data: bytes) -> None:
    """Write a file through a new temporary file beside it, so a failed write never leaves part of it at dest."""
    partial = os.path.join(os.path.dirname(dest), f".{uuid.uuid4().hex}.part")
    # "x" claims a new name: a file already there raises instead of being truncated, and is never removed.
    with open(partial, "xb"):
        pass
    try:
        with open(partial, "wb") as file:
            file.write(data)
        os.replace(partial, dest)
    except BaseException:
        # The write's own error is the one to report, not a failed cleanup.
        with contextlib.suppress(OSError):
            os.remove(partial)
        raise


def _strip_pictures(path: str, audio: FLAC, cover_file: str | None) -> str | None:
    """Remove a FLAC's pictures and padding, keeping 8 KiB of padding.

    Its front cover is written to the folder first if the folder has no cover file. If that cover cannot be
    saved as a JPEG or PNG file, the FLAC is left as it is, so the only copy of the artwork is not lost.

    Args:
        path: The release folder.
        audio: The FLAC to strip.
        cover_file: The folder's cover file, or None if it has none.

    Returns:
        The folder's cover file afterwards, or None if it still has none.
    """
    for picture in audio.pictures:
        if picture.type == PictureType.COVER_FRONT and not cover_file:
            cover = _as_cover_file(picture)
            if cover is None:
                click.secho(
                    f"Left {audio.filename} as it is: its front cover could not be read as an image to keep it.",
                    fg="red",
                )
                return cover_file
            extension, data = cover
            cover_file = os.path.join(path, f"cover.{extension}")
            # A partial cover file would pass for the folder's cover on the next run.
            _write_whole_file(cover_file, data)
            click.secho(f"Extracted cover to: {cover_file}", fg="green")

    audio.clear_pictures()
    audio.save(padding=get_8kib_padding)
    return cover_file


def check_embedded_pictures(path: str) -> None:
    """Strip the FLACs whose embedded pictures plus padding exceed RED's 1 MiB limit, or only warn about them.

    Stripping removes every embedded picture and all but 8 KiB of padding, keeping the front cover as the
    folder's cover file if it has none. With image.strip_oversized_pictures off, the files are only listed.

    Args:
        path: The release folder, which must be safe to change.
    """
    oversized = find_oversized_pictures(path)
    if not oversized:
        return
    click.secho(
        "\nEmbedded pictures plus padding exceed RED's 1 MiB limit in these files:",
        fg="yellow",
        bold=True,
    )
    for filename, size in oversized.items():
        excess = humanfriendly.format_size(size - MAX_PICTURES_AND_PADDING, binary=True)
        click.secho(f"  {filename}: {humanfriendly.format_size(size, binary=True)} ({excess} over)", fg="yellow")

    if not cfg.image.strip_oversized_pictures:
        click.secho("Leaving them as they are: strip_oversized_pictures is off.", fg="yellow")
        return

    cover_file = get_cover_from_path(path)
    stripped = 0
    for filename in oversized:
        audio = FLAC(os.path.join(path, filename))
        cover_file = _strip_pictures(path, audio, cover_file)
        stripped += not audio.pictures
    click.secho(f"Stripped {stripped} file(s) to no embedded pictures and 8 KiB of padding.", fg="green")


def compress_pictures(path):
    for filename in get_audio_files(path):
        if not filename.lower().endswith(".flac"):
            continue
        click.secho(f"Processing file: {filename}", fg="blue")
        audio = FLAC(os.path.join(path, filename))

        padding_size = sum(block.length for block in audio.metadata_blocks if block.code == 1)

        cover_sizes = sum(len(picture.write()) for picture in audio.pictures)
        click.secho(
            (
                f"Padding size: {humanfriendly.format_size(padding_size, binary=True)}, "
                f"Cover size: {humanfriendly.format_size(cover_sizes, binary=True)}"
            ),
            fg="cyan",
        )

        cover_file = get_cover_from_path(path)

        if padding_size + cover_sizes > MAX_PICTURES_AND_PADDING:
            click.secho(
                f"Total size ({humanfriendly.format_size(padding_size + cover_sizes, binary=True)}) exceeds 1MiB!",
                fg="yellow",
            )
            cover_file = _strip_pictures(path, audio, cover_file)

        if audio.pictures == []:
            click.secho("Attempting to add external cover...", fg="magenta")
            if not cover_file:
                click.secho("No cover file found!", fg="red")
                continue

            with open(cover_file, "rb") as c:
                data = c.read()

            max_picture_block_size = MAX_PICTURES_AND_PADDING - humanfriendly.parse_size("8KiB")

            # The PICTURE block's own fields (MIME type, dimensions, lengths) count against the limit too, so
            # the image gets what is left once they are written with no data.
            picture = Picture()
            picture.mime = Image.open(cover_file).get_format_mimetype()

            if len(data) <= max_picture_block_size - len(picture.write()):
                click.secho(
                    f"Cover size ({humanfriendly.format_size(len(data), binary=True)}) within limit",
                    fg="bright_green",
                )
            else:
                click.secho(
                    f"Resizing oversized cover ({humanfriendly.format_size(len(data), binary=True)})...",
                    fg="yellow",
                )
                picture.mime = "image/jpeg"
                image = Image.open(cover_file)
                image.thumbnail((1000, 1000))
                data = compress_to_target_size(image, max_picture_block_size - len(picture.write()))
                if data is None:
                    click.secho(f"Could not shrink {cover_file} enough to embed it; leaving it out.", fg="red")
                    continue

            picture.data = data
            picture.type = PictureType.COVER_FRONT
            audio.add_picture(picture)
            audio.save(padding=get_8kib_padding)
            click.secho(f"Saved {filename} with optimized cover", fg="bright_green")
        else:
            click.secho("Existing covers meet size requirements", fg="bright_white")
