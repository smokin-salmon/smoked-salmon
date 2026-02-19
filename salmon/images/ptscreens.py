from pathlib import Path

import aiohttp
import anyio

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

HEADERS: dict[str, str] = {"X-API-Key": cfg.image.ptscreens_key or ""}


class ImageUploader(BaseImageUploader):
    """Image uploader for ptscreens.com."""

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to ptscreens.com.

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

        url = "https://ptscreens.com/api/1/upload"
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, headers=HEADERS, data=data) as resp,
        ):
            if resp.status == 200:
                try:
                    r = await resp.json()
                    image_data = r.get("image")
                    if not image_data:
                        raise ImageUploadFailed(f"Missing image data in response: {r}")
                    result_url = image_data.get("url")
                    if not result_url:
                        raise ImageUploadFailed(f"Missing image URL in response: {r}")
                    return result_url, None
                except ValueError as e:
                    content = await resp.read()
                    raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{content}") from e
            else:
                content = await resp.read()
                raise ImageUploadFailed(f"Failed. Status {resp.status}:\n{content}")
