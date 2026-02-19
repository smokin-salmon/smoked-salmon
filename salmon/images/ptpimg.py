from pathlib import Path

import aiohttp
import anyio
import msgspec.json

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

HEADERS = {
    "referer": "https://ptpimg.me/index.php",
    "User-Agent": cfg.upload.user_agent,
}


class ImageUploader(BaseImageUploader):
    """Image uploader for ptpimg.me."""

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to ptpimg.me.

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
        data.add_field("api_key", cfg.image.ptpimg_key)
        data.add_field("file-upload[0]", file_data, filename=Path(filename).name)

        url = "https://ptpimg.me/upload.php"
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, headers=HEADERS, data=data) as resp,
        ):
            content = await resp.read()
            if resp.status != 200:
                raise ImageUploadFailed(f"Failed. Status {resp.status}:\n{content}")
            try:
                parsed = msgspec.json.decode(content)
                r = parsed[0]
                return (
                    f"https://ptpimg.me/{r['code']}.{r['ext']}",
                    None,
                )
            except (
                msgspec.DecodeError,
                KeyError,
                IndexError,
            ) as e:
                raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{content}") from e
