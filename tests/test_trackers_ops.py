import sys
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.common import UploadFiles
from salmon.errors import RequestError
from salmon.trackers.base import BaseGazelleApi, HttpResponse
from salmon.trackers.ops import OpsApi


def _split_answer(monkeypatch, answer: bool) -> tuple[list[str], list[dict]]:
    """The Split questions asked, and the forms sent, with each question answered `answer`."""
    asked: list[str] = []
    sent: list[dict] = []

    async def fake_confirm(text, *_args, **_kwargs):
        asked.append(text)
        return answer

    async def fake_upload(self, data, files):
        sent.append(data)
        return 123, 456

    # Patched where OPS asks, whichever way the question is answered (the CLI's click.confirm, or the web's card).
    monkeypatch.setattr("salmon.trackers.ops.interaction.confirm", fake_confirm)
    monkeypatch.setattr(BaseGazelleApi, "upload", fake_upload)
    return asked, sent


def test_ops_split_prompt_ignores_guest_only_second_artist(monkeypatch) -> None:
    tracker = OpsApi()
    asked, _sent = _split_answer(monkeypatch, False)

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
    assert asked == []


def _form(tracker: OpsApi, **extra) -> dict:
    return {"releasetype": tracker.release_types["Album"], "artists[]": ["A", "B"], "importance[]": [1, 1], **extra}


def test_ops_asks_about_split_for_a_new_group_with_two_main_artists(monkeypatch) -> None:
    tracker = OpsApi()
    asked, sent = _split_answer(monkeypatch, True)

    anyio.run(tracker.upload, _form(tracker), UploadFiles(torrent_data=b"torrent"))

    assert len(asked) == 1
    assert sent[0]["releasetype"] == tracker.release_types["Split"]


def test_ops_doesnt_ask_about_split_for_an_upload_into_an_existing_group(monkeypatch) -> None:
    # A cross-upload sends the group's form, release type included, with the group's id (#910).
    tracker = OpsApi()
    asked, sent = _split_answer(monkeypatch, True)

    anyio.run(tracker.upload, _form(tracker, groupid=5), UploadFiles(torrent_data=b"torrent"))

    assert asked == []
    assert sent[0]["releasetype"] == tracker.release_types["Album"]


def test_a_split_answer_for_a_new_group_leaves_a_later_existing_groups_type(monkeypatch) -> None:
    tracker = OpsApi()
    _asked, sent = _split_answer(monkeypatch, True)

    anyio.run(tracker.upload, _form(tracker), UploadFiles(torrent_data=b"torrent"))
    anyio.run(tracker.upload, _form(tracker, groupid=5), UploadFiles(torrent_data=b"torrent"))

    assert sent[0]["releasetype"] == tracker.release_types["Split"]
    assert sent[1]["releasetype"] == tracker.release_types["Album"]


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
