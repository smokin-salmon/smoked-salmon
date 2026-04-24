from pathlib import Path

import aiohttp
import anyio
import msgspec

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader
from salmon.proxy import session_kwargs

HEADERS = {
    "referer": "https://ptpimg.me/index.php",
    "User-Agent": cfg.upload.user_agent,
}


class ImageUploader(BaseImageUploader):
    """Image uploader for ptpimg.me."""

    proxy_service = "ptpimg"

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
        try:
            async with (
                aiohttp.ClientSession(**session_kwargs(self.proxy_service)) as session,
                session.post(url, headers=HEADERS, data=data) as resp,
            ):
                resp.raise_for_status()
                r = await resp.json(loads=msgspec.json.decode)
                return f"https://ptpimg.me/{r[0]['code']}.{r[0]['ext']}", None
        except (msgspec.DecodeError, KeyError, IndexError) as e:
            raise ImageUploadFailed(f"Failed decoding body: {e}") from e
        except aiohttp.ClientError as e:
            raise ImageUploadFailed(f"Network error: {e}") from e
