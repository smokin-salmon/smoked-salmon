import aiohttp

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

HEADERS = {"X-API-Key": cfg.image.oeimg_key}


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
        with open(filename, "rb") as f:
            file_data = f.read()

        data = aiohttp.FormData()
        data.add_field("source", file_data, filename=filename)

        url = "https://imgoe.download/api/1/upload"
        async with aiohttp.ClientSession() as session, session.post(url, headers=HEADERS, data=data) as resp:
            if resp.status == 200:
                try:
                    r = await resp.json()
                    if "image" in r:
                        result_url = r["image"].get("url")
                        return result_url, None
                    raise ImageUploadFailed("Missing image data in response")
                except ValueError as e:
                    content = await resp.read()
                    raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{content}") from e
            else:
                content = await resp.read()
                raise ImageUploadFailed(f"Failed. Status {resp.status}:\n{content}")
