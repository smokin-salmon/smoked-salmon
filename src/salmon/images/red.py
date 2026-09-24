from pathlib import Path

import anyio

from salmon.errors import ImageUploadFailed, RequestError
from salmon.images.base import BaseImageUploader
from salmon.trackers.red import RedApi


class ImageUploader(BaseImageUploader):
    """Image uploader for RED's internal image host.

    Authenticates with the RED tracker API key, so it needs no key of its own. Uploads go
    through a RED tracker client, so they count against the tracker's rate limit like any
    other request to RED. RED's rules forbid uploading spectrals there, hence it cannot be
    used as the specs_uploader.
    """

    def __init__(self, api: RedApi | None = None) -> None:
        """Initialize the uploader.

        Args:
            api: The RED client to upload through. Without one, the uploader makes its own.
        """
        self.api = api or RedApi()

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to RED's image host.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, deletion_url). RED does not provide a deletion URL.

        Raises:
            ImageUploadFailed: If upload fails.
        """
        if not self.api.api_key:
            raise ImageUploadFailed("The RED image host requires tracker.red.api_key to be set")

        async with await anyio.open_file(filename, "rb") as f:
            image = await f.read()

        try:
            return await self.api.upload_image(Path(filename).name, image), None
        except RequestError as e:
            raise ImageUploadFailed(str(e)) from e
