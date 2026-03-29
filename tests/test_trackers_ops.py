import sys
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.common import UploadFiles
from salmon.trackers.base import BaseGazelleApi
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
