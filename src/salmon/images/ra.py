from pathlib import Path

import aiohttp
import anyio
import msgspec

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

UPLOAD_URL = "https://thesungod.xyz/api/image/upload"


class ImageUploader(BaseImageUploader):
    """Image uploader for thesungod.xyz (Ra)."""

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to thesungod.xyz.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, deletion_url).

        Raises:
            ImageUploadFailed: If upload fails.
        """
        async with await anyio.open_file(filename, "rb") as f:
            file_data = await f.read()

        data = aiohttp.FormData()
        data.add_field("api_key", cfg.image.ra_key)
        data.add_field("image", file_data, filename=Path(filename).name)

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(UPLOAD_URL, data=data) as resp,
            ):
                resp.raise_for_status()
                r = await resp.json(loads=msgspec.json.decode)
                return r["links"][0], None
        except (msgspec.DecodeError, KeyError, IndexError) as e:
            raise ImageUploadFailed(f"Failed decoding body: {e}") from e
        except aiohttp.ClientError as e:
            raise ImageUploadFailed(f"Network error: {e}") from e
