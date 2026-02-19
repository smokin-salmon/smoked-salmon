from pathlib import Path
from random import choice

import aiohttp
import anyio

from salmon.constants import UAGENTS
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

HEADERS = {
    "User-Agent": choice(UAGENTS),
    "referrer": "https://catbox.moe/",
}


class ImageUploader(BaseImageUploader):
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
        async with aiohttp.ClientSession() as session, session.post(url, headers=HEADERS, data=data) as resp:
            if resp.status == 200:
                try:
                    return await resp.text(), None
                except ValueError as e:
                    content = await resp.read()
                    raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{content}") from e
            else:
                content = await resp.read()
                raise ImageUploadFailed(f"Failed. Status {resp.status}:\n{content}")
