from pathlib import Path

import aiohttp
import anyio
import msgspec

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images import base as images_base
from salmon.images.base import BaseImageUploader

UPLOAD_URL = "https://thesungod.xyz/api/image/upload"
# The domain its uploads' URLs are on (or a subdomain of it): how a URL given for an image is known as Ra's.
IMAGE_DOMAINS = ("thesungod.xyz",)


class ImageUploader(BaseImageUploader):
    """Image uploader for thesungod.xyz (Ra)."""

    proxy_service = "ra"

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to thesungod.xyz.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, deletion_url).

        Raises:
            ImageUploadFailed: If upload fails.
        """
        key = cfg.image.ra_key
        if not key:
            raise ImageUploadFailed("Ra needs an API key: set ra_key under [image].")
        async with await anyio.open_file(filename, "rb") as f:
            file_data = await f.read()

        data = aiohttp.FormData()
        data.add_field("api_key", key)
        data.add_field("image", file_data, filename=Path(filename).name)

        try:
            async with (
                self._http_session() as session,
                session.post(UPLOAD_URL, data=data) as resp,
            ):
                body = await resp.text()
                if resp.status >= 400:
                    # Masked whatever its length: a server that echoes the request would repeat the key.
                    shown = body.replace(key, "[REDACTED]")
                    raise ImageUploadFailed(f"Ra returned {resp.status}: {shown[:200]}")
                resp_data = msgspec.json.decode(body)
                return resp_data["links"][0], None
        except (msgspec.DecodeError, KeyError, IndexError, TypeError) as e:
            raise ImageUploadFailed(f"Failed decoding body: {e}") from e
        except TimeoutError as e:
            raise ImageUploadFailed(images_base.describe_upload_timeout(e)) from e
        except aiohttp.ClientError as e:
            raise ImageUploadFailed(f"Network error: {e}") from e
