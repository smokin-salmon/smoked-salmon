import mimetypes
from random import choice

import aiohttp
import anyio
from bs4 import BeautifulSoup

from salmon.constants import UAGENTS
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

mimetypes.init()

HEADERS = {
    "User-Agent": choice(UAGENTS),
    "referrer": "https://jerking.empornium.ph/",
    "Accept": "application/json",
    "Linx-Expiry": "0",
}
AUTH_TOKEN = None
COOKIES = {"AGREE_CONSENT": "1", "PHPSESSID": "45onca6s8hi8oi07ljqla31gfu"}


class ImageUploader(BaseImageUploader):
    """Image uploader for EMP (jerking.empornium.ph)."""

    def __init__(self) -> None:
        """Initialize uploader. Auth token is fetched lazily on first upload."""
        self.auth_token: str | None = None

    async def _ensure_auth_token(self) -> str:
        """Fetch auth token from EMP if not already cached.

        Returns:
            The auth token string.

        Raises:
            ImageUploadFailed: If auth token cannot be fetched.
        """
        global AUTH_TOKEN
        if AUTH_TOKEN:
            self.auth_token = AUTH_TOKEN
            return AUTH_TOKEN

        async with (
            aiohttp.ClientSession(cookies=COOKIES) as session,
            session.get("https://jerking.empornium.ph") as resp,
        ):
            text = await resp.text()
            soup = BeautifulSoup(text, "lxml")
            token_elem = soup.find(attrs={"name": "auth_token"})
            if not token_elem or "value" not in token_elem.attrs:
                raise ImageUploadFailed("Failed to fetch auth token from EMP")
            token_value = token_elem["value"]
            if isinstance(token_value, list):
                token_value = token_value[0]
            AUTH_TOKEN = str(token_value)
            self.auth_token = AUTH_TOKEN
            return AUTH_TOKEN

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to EMP.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, deletion_url).

        Raises:
            ImageUploadFailed: If upload fails.
            ValueError: If file is not an image.
        """
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type or mime_type.split("/")[0] != "image":
            raise ValueError(f"Unknown image file type {mime_type}")

        await self._ensure_auth_token()

        async with await anyio.open_file(filename, "rb") as f:
            file_data = await f.read()

        data = aiohttp.FormData()
        data.add_field("action", "upload")
        data.add_field("type", "file")
        data.add_field("auth_token", self.auth_token)
        data.add_field("source", file_data, filename=filename, content_type=mime_type)

        url = "https://jerking.empornium.ph/json"
        async with (
            aiohttp.ClientSession(cookies=COOKIES) as session,
            session.post(url, headers=HEADERS, data=data) as resp,
        ):
            if resp.status == 200:
                try:
                    resp_data = await resp.json()
                    return resp_data["image"]["url"], None
                except (ValueError, KeyError) as e:
                    content = await resp.read()
                    raise ImageUploadFailed(f"Failed decoding body:\n{e}\n{content}") from e
            else:
                content = await resp.read()
                raise ImageUploadFailed(f"Failed. Status {resp.status}:\n{content}")
