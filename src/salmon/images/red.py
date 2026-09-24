from pathlib import Path

import aiohttp
import anyio
import msgspec

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

UPLOAD_URL = "https://redacted.sh/ajax.php?action=upload_image"


class ImageUploader(BaseImageUploader):
    """Image uploader for RED's internal image host.

    Authenticates with the RED tracker API key, so it needs no key of its own.
    RED's rules forbid uploading spectrals there, hence it cannot be used as the
    specs_uploader.
    """

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to RED's image host.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, deletion_url). RED does not provide a deletion URL.

        Raises:
            ImageUploadFailed: If upload fails.
        """
        api_key = cfg.tracker.red.api_key if cfg.tracker.red else None
        if not api_key:
            raise ImageUploadFailed("The RED image host requires tracker.red.api_key to be set")

        async with await anyio.open_file(filename, "rb") as f:
            file_data = await f.read()

        data = aiohttp.FormData()
        data.add_field("file", file_data, filename=Path(filename).name)

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(UPLOAD_URL, headers={"Authorization": api_key}, data=data) as resp,
            ):
                # RED answers a rejected image with HTTP 400 and a JSON body explaining
                # why, so the body is parsed before the status code is considered.
                r = await resp.json(loads=msgspec.json.decode, content_type=None)
                if r.get("status") != "success":
                    raise ImageUploadFailed(f"RED rejected the image: {r.get('error', 'unknown error')}")
                return r["response"]["url"], None
        except (msgspec.DecodeError, ValueError, KeyError, TypeError) as e:
            raise ImageUploadFailed(f"Failed decoding body: {e}") from e
        except aiohttp.ClientError as e:
            raise ImageUploadFailed(f"Network error: {e}") from e
