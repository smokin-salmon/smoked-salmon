import pytest

from salmon.common import UploadFiles
from salmon.errors import LoginError
from salmon.trackers.red import RedApi


@pytest.mark.anyio
async def test_red_upload_falls_back_to_api_when_cookie_upload_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = RedApi()
    tracker.api_key = "test-api-key"

    files = UploadFiles(torrent_data=b"torrent", log_files=[("rip.log", b"log")])
    expected = (123, 456)

    async def fail_site_page_upload(data: dict, files: UploadFiles) -> tuple[int, int]:
        raise LoginError

    async def ok_api_upload(data: dict, files: UploadFiles) -> tuple[int, int]:
        return expected

    monkeypatch.setattr(tracker, "site_page_upload", fail_site_page_upload)
    monkeypatch.setattr(tracker, "api_key_upload", ok_api_upload)

    assert await tracker.upload({}, files) == expected
