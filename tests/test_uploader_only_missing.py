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

import salmon.uploader
from salmon import interaction
from salmon.errors import UploadOnlyMissing
from salmon.uploader import dupe_checker
from salmon.uploader.missing_formats import holdings_for_turn

pytestmark = pytest.mark.usefixtures("image_uploads")

WEB_V0 = _torrent(14, fmt="MP3", encoding="V0 (VBR)")
FULL = ["FLAC Lossless", "MP3 320", "MP3 V0 (VBR)"]


def _as_search_row(torrent: dict[str, Any]) -> dict[str, Any]:
    row = {key: value for key, value in torrent.items() if key not in ("id", "remasterRecordLabel")}
    return {**row, "torrentId": torrent["id"]}


class TwoSites(GroupTracker):
    """The fake tracker, whose group holds `held[site]` on each site, recording which site sent each request."""

    def __init__(self, held: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__([], found=True)
        self._held = held
        self.sites: list[str] = []  # The site of each of self.sent, in order

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        key = request.headers.get("Authorization")
        site = next((code for code, api_key in API_KEYS.items() if api_key and api_key == key), "?")
        held = self._held.get(site, [])
        # As a search row names them: the id as torrentId, and no label.
        browse = request.path == "/ajax.php" and request.query.get("action") == "browse"
        self._group_torrents = [_as_search_row(torrent) for torrent in held] if browse else held
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


# What the code review of #1169 found


def test_a_tracker_answered_before_a_listed_first_tracker_leaves_no_answer_behind(monkeypatch, tmp_path) -> None:
    # RED holds the FLAC and "m" is taken, then the review leaves the release on RED's Do-Not-Upload list: OPS, which
    # holds nothing, is answered on its own and gets everything.
    calls = Counter[str]()

    def refusal(tracker: str, _release: dict[str, Any], said: str | None = None) -> str | None:
        calls[tracker] += 1
        return "listed" if tracker == "RED" and calls[tracker] > 1 else None

    monkeypatch.setattr(salmon.uploader, "_do_not_upload_refusal", refusal)
    run = _two_trackers(monkeypatch, tmp_path, {"RED": [WEB_FLAC]})

    assert run.result.exit_code == 0, run.result.output
    assert _uploads(run.tracker, "RED") == []
    assert _uploads(run.tracker, "OPS") == FULL


def test_spectrals_after_skips_a_tracker_holding_the_source(monkeypatch, tmp_path) -> None:
    # Its check runs on our FLAC once it is up, as --skip-flac-upload with --spectrals-after is refused.
    run = _up(
        monkeypatch,
        tmp_path,
        TwoSites({"RED": [WEB_FLAC]}),
        args=("--spectrals-after",),
        trackers=("RED",),
        input="1\n\n" + "\n" * 10,
    )

    assert run.result.exit_code == 0, run.result.output
    assert "Not uploading only the missing formats to RED with --spectrals-after" in run.result.output
    assert _uploads(run.tracker, "RED") == []


def test_several_held_sources_are_not_guessed_with_yyy(monkeypatch, tmp_path) -> None:
    run = _two_trackers(monkeypatch, tmp_path, {"RED": [WEB_FLAC, {**WEB_FLAC, "id": 15}]})

    output = run.result.output
    assert run.result.exit_code == 0, output
    assert "Not picking the FLAC the transcodes are made from with -yyy" in output
    assert _uploads(run.tracker, "RED") == []
    assert _uploads(run.tracker, "OPS") == FULL


def test_every_offered_conversion_is_decided_and_only_the_default_ones_need_to_be_held(monkeypatch) -> None:
    rls_data = {"format": "FLAC", "encoding": "Lossless"}
    track_data = {"01.flac": {"sample rate": 44100}}
    group = {"group": {"id": 7, "year": 2020}, "torrents": [WEB_FLAC, WEB_V0]}
    release = {"source": "WEB", "format": "FLAC", "encoding": "Lossless", "year": 2020}

    holdings = holdings_for_turn("RED", group, release, rls_data, track_data, ("320",))

    # V0 is not made by default, but the conversion question may pick it: it is a dupe there.
    assert holdings.held_conversions == {"MP3 V0"}
    assert not holdings.nothing_missing
    with_320 = {**group, "torrents": [WEB_FLAC, WEB_320]}
    assert holdings_for_turn("RED", with_320, release, rls_data, track_data, ("320",)).nothing_missing


# The group question


class QuestionSite:
    """The group question's tracker: the group it fetches, counted."""

    site_string = "RED"

    def __init__(self, torrents: list[dict[str, Any]]) -> None:
        self.torrents = torrents
        self.fetches = 0

    async def torrentgroup(self, group_id: int) -> dict[str, Any]:
        self.fetches += 1
        info = {"id": group_id, "name": "Album", "year": 2020, "musicInfo": {"artists": [{"name": "Artist"}]}}
        return {"group": info, "torrents": self.torrents}


RELEASE = {"source": "WEB", "format": "FLAC", "encoding": "Lossless", "year": 2020}


def _row(torrents: list[dict[str, Any]]) -> dict[str, Any]:
    """A search row: its torrents as a search names them."""
    rows = [_as_search_row(torrent) for torrent in torrents]
    return {"groupId": 5, "artist": "Artist", "groupName": "Album", "groupYear": 2020, "torrents": rows}


def _ask(site: QuestionSite, answer: str | None = None) -> tuple[Any, list[Any], list[str]]:
    """Ask the group question for a search row, with -yyy; the outcome, the default offered and the text asked."""
    defaults: list[Any] = []
    asked: list[str] = []

    async def prompt(text: str, *, default: Any = None, **_kwargs: Any) -> Any:
        defaults.append(default)
        asked.append(click.unstyle(text))
        return answer or default

    async def ask() -> Any:
        with interaction.assuming_defaults(True):
            try:
                return await dupe_checker._confirm_group_id(
                    site,  # pyright: ignore[reportArgumentType]
                    5,
                    [_row(site.torrents)],
                    False,
                    RELEASE,
                    offer_missing=True,
                )
            except (UploadOnlyMissing, click.Abort) as outcome:
                return outcome

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dupe_checker.interaction, "prompt", prompt)
        return anyio.run(ask), defaults, asked


def test_a_held_source_defaults_to_upload_only_what_is_missing_with_the_group_fetched() -> None:
    site = QuestionSite([WEB_FLAC])
    answer, defaults, asked = _ask(site)

    assert defaults == ["m"]
    assert isinstance(answer, UploadOnlyMissing)
    assert answer.group_id == 5
    # The search row names the torrent torrentId: the group is fetched, once, for the held torrent's id.
    assert answer.group["torrents"] == [WEB_FLAC]
    assert site.fetches == 1
    assert CONFIRM_GROUP in asked[0]
    assert "upload only the [m]issing formats" in asked[0]


def test_nothing_held_defaults_to_yes_and_fetches_nothing() -> None:
    site = QuestionSite([WEB_320])
    answer, defaults, _asked = _ask(site)

    assert defaults == ["Y"]
    assert answer is True
    assert site.fetches == 0


def test_abort_is_still_there_by_hand() -> None:
    answer, _defaults, _asked = _ask(QuestionSite([WEB_FLAC]), answer="a")
    assert isinstance(answer, click.Abort)
