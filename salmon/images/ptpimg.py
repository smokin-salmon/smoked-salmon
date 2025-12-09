import aiohttp

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

HEADERS = {"referer": "https://ptpimg.me/index.php", "User-Agent": cfg.upload.user_agent}


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
        with open(filename, "rb") as f:
            file_data = f.read()

        data = aiohttp.FormData()
        data.add_field("api_key", cfg.image.ptpimg_key)
        data.add_field("file-upload[0]", file_data, filename=filename)

        url = "https://ptpimg.me/upload.php"
        async with aiohttp.ClientSession() as session, session.post(url, headers=HEADERS, data=data) as resp:
            if resp.status == 200:
                try:
                    r = (await resp.json())[0]
                    return f"https://ptpimg.me/{r['code']}.{r['ext']}", None
                except (ValueError, KeyError, IndexError) as e:
                    content = await resp.read()
                    raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{content}") from e
            else:
                content = await resp.read()
                raise ImageUploadFailed(f"Failed. Status {resp.status}:\n{content}")
