from pathlib import Path

import aiohttp
import anyio
import msgspec

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

HEADERS: dict[str, str] = {"X-API-Key": cfg.image.oeimg_key or ""}


class ImageUploader(BaseImageUploader):
    """Image uploader for imgoe.download (oeimg)."""

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to oeimg.

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
        data.add_field("source", file_data, filename=Path(filename).name)

        url = "https://imgoe.download/api/1/upload"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(url, headers=HEADERS, data=data) as resp,
            ):
                resp.raise_for_status()
                r = await resp.json(loads=msgspec.json.decode)
                return r["image"]["url"], None
        except (ValueError, KeyError) as e:
            raise ImageUploadFailed(f"Failed decoding body: {e}") from e
        except aiohttp.ClientError as e:
            raise ImageUploadFailed(f"Network error: {e}") from e
