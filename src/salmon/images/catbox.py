from pathlib import Path
from random import choice

import aiohttp
import anyio

from salmon.constants import UAGENTS
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader
from salmon.proxy import session_kwargs

HEADERS = {
    "User-Agent": choice(UAGENTS),
    "referrer": "https://catbox.moe/",
}


class ImageUploader(BaseImageUploader):
    proxy_service = "catbox"

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to catbox.moe.

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
        data.add_field("reqtype", "fileupload")
        data.add_field("userhash", "")
        data.add_field("fileToUpload", file_data, filename=Path(filename).name)
        url = "https://catbox.moe/user/api.php"
        try:
            async with (
                aiohttp.ClientSession(**session_kwargs(self.proxy_service)) as session,
                session.post(url, headers=HEADERS, data=data) as resp,
            ):
                resp.raise_for_status()
                return await resp.text(), None
        except ValueError as e:
            raise ImageUploadFailed(f"Failed decoding body: {e}") from e
        except aiohttp.ClientError as e:
            raise ImageUploadFailed(f"Network error: {e}") from e
