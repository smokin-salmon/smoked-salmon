from pathlib import Path

import aiohttp
import anyio
import msgspec.json

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

HEADERS = {"referer": "https://imgbb.com/", "User-Agent": cfg.upload.user_agent}


class ImageUploader(BaseImageUploader):
    """Image uploader for imgbb.com."""

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to imgbb.com.

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
        data.add_field("key", cfg.image.imgbb_key)
        data.add_field("image", file_data, filename=Path(filename).name)

        url = "https://api.imgbb.com/1/upload"
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, headers=HEADERS, data=data) as resp,
        ):
            content = await resp.read()
            if resp.status != 200:
                raise ImageUploadFailed(f"Failed. Status {resp.status}:\n{content}")
            try:
                resp_data = msgspec.json.decode(content)
                return resp_data["data"]["url"], None
            except (
                msgspec.DecodeError,
                KeyError,
                TypeError,
            ) as e:
                raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{content}") from e
