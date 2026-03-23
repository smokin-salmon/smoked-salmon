import unittest
from unittest.mock import AsyncMock

from salmon.common import UploadFiles
from salmon.errors import LoginError
from salmon.trackers.red import RedApi


class TestRedUploadFallback(unittest.IsolatedAsyncioTestCase):
    async def test_red_upload_falls_back_to_api_when_cookie_upload_fails(self) -> None:
        tracker = RedApi()
        tracker.api_key = "test-api-key"

        files = UploadFiles(torrent_data=b"torrent", log_files=[("rip.log", b"log")])
        expected = (123, 456)

        tracker.site_page_upload = AsyncMock(side_effect=LoginError)  # type: ignore[method-assign]
        tracker.api_key_upload = AsyncMock(return_value=expected)  # type: ignore[method-assign]

        self.assertEqual(await tracker.upload({}, files), expected)
