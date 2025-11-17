import asyncio
import contextlib
from typing import Optional, Tuple

import pyimgbox

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader


class ImageUploader(BaseImageUploader):
    """
    Uploads a single image to imgbox.com using the pyimgbox library.

    Returns the direct URL to the full-size image by default, or the thumbnail URL
    if USE_THUMBNAIL is set to True.
    """

    THUMB_WIDTH = 350  # Default thumbnail width in pixels (imgbox default)
    USE_THUMBNAIL = False  # False → full-size URL; True → thumbnail URL

    def upload_file(self, filename: str) -> Tuple[str, Optional[str]]:
        """
        Public entry point required by BaseImageUploader.

        Opens the file at the given path, ensures it is properly closed after upload,
        and returns the resulting image URL.

        Args:
            filename: Path to the image file on disk.

        Returns:
            Tuple of (direct_image_url_or_thumbnail_url, None)
        """
        with contextlib.ExitStack():
            return self._perform(filename)

    def _perform(self, filename: str) -> Tuple[str, Optional[str]]:
        """
        Executes the actual upload in a new asyncio event loop.

        Creates a temporary event loop since this method is called synchronously,
        runs the async upload, and cleans up properly.

        Args:
            filename: Path to the image file to upload.

        Returns:
            Tuple containing the final image/thumbnail URL and None.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            url = loop.run_until_complete(self._upload_single(filename))
            return url, None
        except Exception as e:
            raise ImageUploadFailed(f"ImgBox upload failed: {e}") from e
        finally:
            loop.close()

    async def _upload_single(self, filename: str) -> str:
        """
        Asynchronously uploads a single image file to imgbox.com.

        Uses a temporary single-image gallery titled "salmon-upload".
        The gallery is automatically cleaned up on exit.

        Args:
            filename: Filesystem path to the image to upload.

        Returns:
            The direct full-size image URL or thumbnail URL based on USE_THUMBNAIL.

        Raises:
            ImageUploadFailed: If the upload fails or returns an error.
        """
        async with pyimgbox.Gallery(
            title="salmon-upload",
            thumb_width=self.THUMB_WIDTH,
            adult=getattr(cfg.upload, "imgbox_adult", False),
        ) as gallery:
            submission = await gallery.upload(filepath=filename)

            if not submission["success"]:
                error_msg = submission.get("error", "Unknown error")
                raise ImageUploadFailed(
                    f"ImgBox upload failed for {filename}: {error_msg}"
                )

            return (
                submission["thumbnail_url"]
                if self.USE_THUMBNAIL
                else submission["image_url"]
            )