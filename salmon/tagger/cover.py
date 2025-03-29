import imghdr
import os
import re

import click
import requests

from salmon import config


def download_cover_if_nonexistent(path, cover_url):
    for filename in os.listdir(path):
        if re.match(r"^(cover|folder)\.(jpe?g|png)$", filename, flags=re.IGNORECASE):
            return
    if cover_url:
        click.secho("\nDownloading cover image...", fg="yellow")
        _download_cover(path, cover_url)


def _download_cover(path, cover_url):
    ext = os.path.splitext(cover_url)[1]
    c = "c" if config.LOWERCASE_COVER else "C"
    headers = {'User-Agent': 'smoked-salmon-v1'}
    stream = requests.get(cover_url, stream=True, headers=headers)
    if stream.status_code < 400:
        cover_path = os.path.join(path, f"{c}over{ext}")
        with open(cover_path, "wb") as f:
            for chunk in stream.iter_content(chunk_size=5096):
                if chunk:
                    f.write(chunk)

        img_type = imghdr.what(cover_path)
        if img_type not in ["jpeg", "png"]:
            os.remove(cover_path)
            click.secho("\nFailed to download cover image (ERROR downloaded file is not an image [JPEG, PNG])", fg="red")
    else:
        click.secho(f"\nFailed to download cover image (ERROR {stream.status_code})", fg="red")
