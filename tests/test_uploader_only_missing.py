"""Upload only what each tracker's edition is missing (#1169).

When a tracker's edition already holds our format, the group question defaults to uploading only what it is missing,
-yyy included: the held formats are left out on that tracker, a tracker holding everything is skipped, and the run
goes on to the next tracker instead of aborting. The runs go against the local fake tracker of
helpers.uploader_dry_run, never a real one; it serves both sites and tells them apart by their API key.
"""

from collections import Counter
from typing import Any

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from helpers.uploader_dry_run import API_KEYS, FakeTracker
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)
from test_uploader_reviewed_edition import CONFIRM_GROUP, WEB_320, WEB_FLAC, GroupTracker, _torrent, _up

from salmon import interaction
from salmon.errors import UploadOnlyMissing
from salmon.uploader import dupe_checker

pytestmark = pytest.mark.usefixtures("image_uploads")

WEB_V0 = _torrent(14, fmt="MP3", encoding="V0 (VBR)")
FULL = ["FLAC Lossless", "MP3 320", "MP3 V0 (VBR)"]


class TwoSites(GroupTracker):
    """The fake tracker, whose group holds `held[site]` on each site, recording which site sent each request."""

    def __init__(self, held: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__([], found=True)
        self._held = held
        self.sites: list[str] = []  # The site of each of self.sent, in order

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        key = request.headers.get("Authorization")
        site = next((code for code, api_key in API_KEYS.items() if api_key and api_key == key), "?")
        self._group_torrents = self._held.get(site, [])
        answer = await super()._handle(request)
        self.sites += [site] * (len(self.sent) - len(self.sites))
        return answer


def _uploads(tracker: FakeTracker, site: str) -> list[str]:
    assert isinstance(tracker, TwoSites)
    return [
        f"{dict(sent.fields)['format']} {dict(sent.fields)['bitrate']}"
        for sent, sent_site in zip(tracker.sent, tracker.sites, strict=True)
        if sent_site == site and sent.method == "POST" and sent.query.get("action") == "upload"
    ]


def _requests(tracker: FakeTracker) -> Counter[tuple[str, str, str]]:
    assert isinstance(tracker, TwoSites)
    return Counter(
        (site, sent.method, sent.query.get("action") or sent.path)
        for sent, site in zip(tracker.sent, tracker.sites, strict=True)
    )


def _two_trackers(monkeypatch, tmp_path, held: dict[str, list[dict[str, Any]]], red_answer: str = ""):
    # RED, then OPS, named with -t: for each, the group (1) the search finds, then the group question.
    answers = f"1\n{red_answer}\n1\n\n" + "\n" * 10
    return _up(monkeypatch, tmp_path, TwoSites(held), trackers=("RED", "OPS"), input=answers)


def test_a_tracker_holding_the_source_gets_only_the_missing_conversions(monkeypatch, tmp_path) -> None:
    run = _two_trackers(monkeypatch, tmp_path, {"RED": [WEB_FLAC]})

    output = run.result.output
    assert run.result.exit_code == 0, output
    assert "Aborting" not in output
    assert _uploads(run.tracker, "RED") == ["MP3 320", "MP3 V0 (VBR)"]
    assert _uploads(run.tracker, "OPS") == FULL
    assert "Not uploading WEB / FLAC / Lossless to RED: this edition has 2020 / Label / CAT1 / WEB / FLAC" in output
    # RED's transcodes are filed as transcodes of the torrent it holds.
    assert "transcoding from" in output and "torrentid=11" in output


def test_a_tracker_holding_everything_is_skipped_and_the_next_one_uploaded(monkeypatch, tmp_path) -> None:
    run = _two_trackers(monkeypatch, tmp_path, {"RED": [WEB_FLAC, WEB_320, WEB_V0]})

    output = run.result.output
    assert run.result.exit_code == 0, output
    assert "Aborting" not in output
    assert _uploads(run.tracker, "RED") == []
    assert _uploads(run.tracker, "OPS") == FULL
    assert "RED has every format of this upload: skipping it." in output
    for name in ("WEB / FLAC / Lossless", "MP3 320", "MP3 V0"):
        assert f"Not uploading {name}" in output


def test_one_tracker_holding_everything_ends_the_run_without_uploading(monkeypatch, tmp_path) -> None:
    run = _up(
        monkeypatch,
        tmp_path,
        TwoSites({"RED": [WEB_FLAC, WEB_320, WEB_V0]}),
        trackers=("RED",),
        input="1\n\n" + "\n" * 10,
    )

    assert run.result.exit_code == 0, run.result.output
    assert "RED has every format of this upload: skipping it." in run.result.output
    assert _uploads(run.tracker, "RED") == []


def test_the_held_format_is_the_only_request_left_out(monkeypatch, tmp_path) -> None:
    # Before #1169, -yyy aborted the whole run here: what it sends is measured answering yes by hand instead.
    (tmp_path / "yes").mkdir()
    (tmp_path / "missing").mkdir()
    before = _two_trackers(monkeypatch, tmp_path / "yes", {"RED": [WEB_FLAC]}, red_answer="y").tracker
    after = _two_trackers(monkeypatch, tmp_path / "missing", {"RED": [WEB_FLAC]}).tracker

    sent_before, sent_after = _requests(before), _requests(after)
    for request, count in sent_after.items():
        assert count <= sent_before[request], request
    assert _uploads(after, "RED") == ["MP3 320", "MP3 V0 (VBR)"]
    assert _uploads(before, "RED") == FULL
    assert _uploads(after, "OPS") == _uploads(before, "OPS") == FULL
    assert sum(sent_after.values()) < sum(sent_before.values())


# The group question


def _question(monkeypatch, torrents: list[dict[str, Any]]) -> tuple[Any, list[Any]]:
    """Ask the group question for a printed search row, with -yyy; the answer and the default it offered."""
    defaults: list[Any] = []

    async def prompt(_text: str, *, default: Any = None, **_kwargs: Any) -> Any:
        defaults.append(default)
        return default

    monkeypatch.setattr(dupe_checker.interaction, "prompt", prompt)
    row = {"groupId": 5, "artist": "Artist", "groupName": "Album", "groupYear": 2020, "torrents": torrents}
    release = {"source": "WEB", "format": "FLAC", "encoding": "Lossless", "year": 2020}

    async def ask() -> Any:
        with interaction.assuming_defaults(True):
            try:
                return await dupe_checker._confirm_group_id(None, 5, [row], False, release, offer_missing=True)  # pyright: ignore[reportArgumentType]
            except UploadOnlyMissing as chosen:
                return chosen

    return anyio.run(ask), defaults


def test_a_held_source_defaults_to_upload_only_what_is_missing(monkeypatch) -> None:
    answer, defaults = _question(monkeypatch, [WEB_FLAC])
    assert defaults == ["m"]
    assert isinstance(answer, UploadOnlyMissing)
    assert answer.group_id == 5
    assert answer.group["torrents"] == [WEB_FLAC]


def test_nothing_held_defaults_to_yes(monkeypatch) -> None:
    answer, defaults = _question(monkeypatch, [WEB_320])
    assert defaults == ["Y"]
    assert answer is True


def test_abort_is_still_there_by_hand(monkeypatch) -> None:
    async def prompt(_text: str, **_kwargs: Any) -> str:
        return "a"

    monkeypatch.setattr(dupe_checker.interaction, "prompt", prompt)
    row = {"groupId": 5, "artist": "Artist", "groupName": "Album", "groupYear": 2020, "torrents": [WEB_FLAC]}
    release = {"source": "WEB", "format": "FLAC", "encoding": "Lossless", "year": 2020}
    with pytest.raises(click.Abort):
        anyio.run(lambda: dupe_checker._confirm_group_id(None, 5, [row], False, release, offer_missing=True))  # pyright: ignore[reportArgumentType]


def test_the_question_names_the_new_answer(monkeypatch) -> None:
    asked: list[str] = []

    async def prompt(text: str, *, default: Any = None, **_kwargs: Any) -> Any:
        asked.append(click.unstyle(text))
        return default

    monkeypatch.setattr(dupe_checker.interaction, "prompt", prompt)
    row = {"groupId": 5, "artist": "Artist", "groupName": "Album", "groupYear": 2020, "torrents": [WEB_FLAC]}
    release = {"source": "WEB", "format": "FLAC", "encoding": "Lossless", "year": 2020}
    with pytest.raises(UploadOnlyMissing):
        anyio.run(lambda: dupe_checker._confirm_group_id(None, 5, [row], False, release, offer_missing=True))  # pyright: ignore[reportArgumentType]
    assert CONFIRM_GROUP in asked[0]
    assert "upload only the [m]issing formats" in asked[0]
