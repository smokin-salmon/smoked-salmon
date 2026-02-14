import pyimgbox

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader


class ImageUploader(BaseImageUploader):
    """Uploads a single image to imgbox.com using the pyimgbox library.

    Returns the direct URL to the full-size image by default, or the thumbnail URL
    if USE_THUMBNAIL is set to True.
    """

    THUMB_WIDTH = 350  # Default thumbnail width in pixels (imgbox default)
    USE_THUMBNAIL = False  # False → full-size URL; True → thumbnail URL

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload image file to imgbox.com.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, None).

        Raises:
            ImageUploadFailed: If upload fails.
        """
        try:
            async with pyimgbox.Gallery(
                title="salmon-upload",
                thumb_width=self.THUMB_WIDTH,
                adult=getattr(cfg.upload, "imgbox_adult", False),
            ) as gallery:
                submission = await gallery.upload(filepath=filename)

                if not submission["success"]:
                    error_msg = submission.get("error", "Unknown error")
                    raise ImageUploadFailed(f"ImgBox upload failed for {filename}: {error_msg}")

                url = submission["thumbnail_url"] if self.USE_THUMBNAIL else submission["image_url"]
                return url, None
        except Exception as e:
            if isinstance(e, ImageUploadFailed):
                raise
            raise ImageUploadFailed(f"ImgBox upload failed: {e}") from e
