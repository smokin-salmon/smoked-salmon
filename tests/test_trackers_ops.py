import sys
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.common import UploadFiles
from salmon.errors import RequestError
from salmon.trackers.base import BaseGazelleApi, HttpResponse
from salmon.trackers.ops import OpsApi


def test_ops_split_prompt_ignores_guest_only_second_artist(monkeypatch) -> None:
    tracker = OpsApi()
    confirm_called = False

    def fake_confirm(*_args, **_kwargs):
        nonlocal confirm_called
        confirm_called = True
        return False

    async def fake_upload(self, data, files):
        return 123, 456

    monkeypatch.setattr("salmon.trackers.ops.click.confirm", fake_confirm)
    monkeypatch.setattr(BaseGazelleApi, "upload", fake_upload)

    result = anyio.run(
        tracker.upload,
        {
            "releasetype": tracker.release_types["Single"],
            "artists[]": ["Anna Zak", "Itay Galo"],
            "importance[]": [1, 2],
        },
        UploadFiles(torrent_data=b"torrent"),
    )

    assert result == (123, 456)
    assert confirm_called is False


def test_a_failed_lossy_master_report_shows_none_of_its_credentials(monkeypatch) -> None:
    tracker = OpsApi()
    tracker.authkey = "ops-authkey-0123456789"

    async def redirected(*_args, **_kwargs) -> HttpResponse:
        url = f"https://orpheus.network/reportsv2.php?id=5&key={tracker.authkey}"
        return HttpResponse(text="", url=url, status=200)

    monkeypatch.setattr(tracker, "_request", redirected)

    with pytest.raises(RequestError, match="reportsv2.php") as caught:
        anyio.run(tracker.report_lossy_master, 555, "a comment", "CD")
    assert tracker.authkey not in str(caught.value)
