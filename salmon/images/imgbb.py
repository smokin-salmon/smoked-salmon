import aiohttp

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
        with open(filename, "rb") as f:
            file_data = f.read()

        data = aiohttp.FormData()
        data.add_field("key", cfg.image.imgbb_key)
        data.add_field("image", file_data, filename=filename)

        url = "https://api.imgbb.com/1/upload"
        async with aiohttp.ClientSession() as session, session.post(url, headers=HEADERS, data=data) as resp:
            if resp.status == 200:
                try:
                    resp_data = await resp.json()
                    return resp_data["data"]["url"], None
                except (ValueError, KeyError, TypeError) as e:
                    content = await resp.read()
                    raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{content}") from e
            else:
                content = await resp.read()
                raise ImageUploadFailed(f"Failed. Status {resp.status}:\n{content}")
