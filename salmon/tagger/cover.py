import os
import re

import click
import filetype
import requests

from salmon import cfg


def download_cover_if_nonexistent(path, cover_url):
    """
    source folder path, url for cover image to be downloaded from
    returns local source path of cover image, and whether image was downloaded
    """
    # use local file if matches filter
    for filename in os.listdir(path):
        if re.match(r"^(cover|folder)\.(jpe?g|png)$", filename, flags=re.IGNORECASE):
            cover_path = os.path.join(path, filename)
            click.secho(f"\nUsing existing cover image found: {filename}...", fg="yellow")
            return cover_path, False
    # use url provided
    if cover_url:
        click.secho("\nDownloading Cover Image...", fg="yellow")
        cover_path = _download_cover(path, cover_url)
        if cover_path:
            return cover_path, True
    click.secho(
        "\nNo existing Cover Image found in Source Folder, no Cover Image downloaded", fg="red"
    )
    return None, None


def _download_cover(path, cover_url):
    ext = os.path.splitext(cover_url)[1]
    c = "c" if cfg.upload.formatting.lowercase_cover else "C"
    headers = {'User-Agent': 'smoked-salmon-v1'}
    stream = requests.get(cover_url, stream=True, headers=headers)

    if stream.status_code < 400:
        cover_image_filename = c + "over" + ext
        cover_path = os.path.join(path, cover_image_filename)
        with open(cover_path, "wb") as f:
            for chunk in stream.iter_content(chunk_size=5096):
                if chunk:
                    f.write(chunk)

        kind = filetype.guess(cover_path)
        if not kind or kind.mime not in ["image/jpeg", "image/png"]:
            os.remove(cover_path)
            click.secho("\nFailed to download cover image (ERROR file is not an image [JPEG, PNG])", fg="red")
        click.secho(f"Cover image downloaded: {cover_image_filename} ", fg="yellow")
        return cover_path
    else:
        click.secho(f"\nFailed to download cover image (ERROR {stream.status_code})", fg="red")
        return None
