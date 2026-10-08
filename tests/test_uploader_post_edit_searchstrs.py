"""After the metadata review, dupe checks must use the reviewed metadata, not the pre-review rls_data.

_upload_staged() builds rls_data from the file tags before the user reviews and edits metadata
(edit_metadata). If the review changes the artists, title or catno, the dupe checks that run after
it, the last-minute dupe check, the request search and a second tracker's group search, must use
search strings and a title built from the edited metadata. On master they keep using the pre-edit
rls_data (#521), so a renamed release is checked against its old name.

Every step that does not touch this is stubbed out, including the two dupe-check functions whose
own query is already correct (fetch_existing_group_candidates_in_background, recheck_dupe): what is
under test is only what _upload_staged hands to last_min_dupe_check, check_requests and
check_existing_group (for a second tracker), so those three are stubbed to record their arguments.
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest

import salmon.trackers
import salmon.uploader
from salmon.uploader import generate_dupe_check_searchstrs

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


class FakeSite:
    """Stands in for a tracker API instance: never talks to a network."""

    def __init__(self, site_code: str) -> None:
        self.site_code = site_code
        self.site_string = site_code
        self.base_url = f"http://{site_code.lower()}.example"


def _as_gazelle_api(site: FakeSite) -> "BaseGazelleApi":
    return cast("BaseGazelleApi", cast("object", site))


def _returning(result: Any = None):
    def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _returning_async(result: Any = None):
    async def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


class Recorder:
    """Records the arguments each stubbed dupe-check call was made with."""

    def __init__(self) -> None:
        self.recheck_dupe_calls: list[tuple] = []
        self.last_min_dupe_check_calls: list[tuple] = []
        self.check_requests_calls: list[tuple] = []
        self.check_existing_group_calls: list[tuple] = []


def _install(monkeypatch: pytest.MonkeyPatch, rls_data: dict, metadata: dict, recorder: Recorder) -> None:
    path = "/release"

    async def fake_recheck_dupe(gazelle_site, searchstrs, md):
        recorder.recheck_dupe_calls.append((gazelle_site, list(searchstrs), md))
        return None

    async def fake_last_min_dupe_check(gazelle_site, searchstrs, our_title=None):
        recorder.last_min_dupe_check_calls.append((gazelle_site, list(searchstrs), our_title))

    async def fake_check_requests(gazelle_site, searchstrs):
        recorder.check_requests_calls.append((gazelle_site, list(searchstrs)))
        return None

    async def fake_check_existing_group(gazelle_site, searchstrs, offer_deletion=True, our_title=None, release=None):
        recorder.check_existing_group_calls.append((gazelle_site, list(searchstrs), our_title))
        return None

    @asynccontextmanager
    async def fake_group_candidates_in_background(*_args, **_kwargs):
        yield None

    async def fake_upload_and_report(*_args, **_kwargs):
        return 1, 5, None, None, "url"

    class FakeUploadManager:
        async def execute_upload(self) -> None:
            pass

    def fake_confirm(text: str, *_args, **_kwargs) -> bool:
        # Only the last-minute dupe check's own confirm (stubbed away here) would need "yes"; every
        # other confirm in the flow (downconversion options) should decline, to keep the flow simple.
        return False

    for name, fake in {
        "gather_audio_info": _returning({}),
        "check_hybrid": _returning(False),
        "standardize_tags": _returning(),
        "gather_tags": _returning({}),
        "construct_rls_data": _returning_async(rls_data),
        "check_spectrals": _returning_async((False, None)),
        "get_metadata": _returning_async((dict(rls_data, cover=None), None)),
        "edit_metadata": _returning_async((path, metadata, {}, {})),
        "concat_track_data": _returning({}),
        "resolve_cover_url": _returning_async((True, None)),
        "print_torrents": _returning_async(None),
        "UploadManager": FakeUploadManager,
        "upload_and_report": fake_upload_and_report,
        "fetch_existing_group_candidates_in_background": fake_group_candidates_in_background,
        "recheck_dupe": fake_recheck_dupe,
        "last_min_dupe_check": fake_last_min_dupe_check,
        "check_requests": fake_check_requests,
        "check_existing_group": fake_check_existing_group,
    }.items():
        monkeypatch.setattr(salmon.uploader, name, fake)

    monkeypatch.setattr(salmon.uploader.click, "confirm", fake_confirm)
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", False)
    monkeypatch.setattr(salmon.uploader.cfg.upload.requests, "last_minute_dupe_check", True)
    monkeypatch.setattr(salmon.uploader.cfg.upload.requests, "check_requests", True)
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", True)
    monkeypatch.setattr(salmon.uploader.cfg.image, "auto_compress_cover", False)

    sites = {"FAKE1": FakeSite("FAKE1"), "FAKE2": FakeSite("FAKE2")}
    monkeypatch.setattr(salmon.trackers, "tracker_list", list(sites))

    chosen = iter(["FAKE2", None])

    async def fake_choose_tracker(_remaining):
        return next(chosen)

    def fake_get_class(code: str):
        return lambda: sites[code]

    monkeypatch.setattr(salmon.trackers, "choose_tracker", fake_choose_tracker)
    monkeypatch.setattr(salmon.trackers, "get_class", fake_get_class)


def test_post_review_dupe_checks_use_the_edited_metadata_not_the_pre_review_rls_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rls_data = {
        "format": "FLAC",
        "encoding": "Lossless",
        "artists": [("Artist", "main")],
        "title": "Old Title",
        "catno": None,
    }
    metadata = {**rls_data, "title": "New Title", "cover": None}
    recorder = Recorder()
    _install(monkeypatch, rls_data, metadata, recorder)

    old_searchstrs = generate_dupe_check_searchstrs(rls_data["artists"], rls_data["title"], rls_data["catno"])
    new_searchstrs = generate_dupe_check_searchstrs(metadata["artists"], metadata["title"], metadata["catno"])
    assert old_searchstrs != new_searchstrs

    site = FakeSite("FAKE1")
    anyio.run(salmon.uploader.upload, _as_gazelle_api(site), "/release", None, "WEB", None, (), None)

    # recheck_dupe is handed the pre-review search strings, to compare against what the edited
    # metadata gives: that part is already correct on master and is untouched here.
    assert len(recorder.recheck_dupe_calls) == 1
    _, searchstrs, md = recorder.recheck_dupe_calls[0]
    assert searchstrs == old_searchstrs
    assert md is metadata

    # The last-minute dupe check, the request search (both trackers) and the second tracker's
    # group search must all use the edited title, not the pre-review one.
    assert len(recorder.last_min_dupe_check_calls) == 1
    _, searchstrs, our_title = recorder.last_min_dupe_check_calls[0]
    assert searchstrs == new_searchstrs
    assert our_title == "New Title"

    assert len(recorder.check_requests_calls) == 2
    for _, searchstrs in recorder.check_requests_calls:
        assert searchstrs == new_searchstrs

    assert len(recorder.check_existing_group_calls) == 1
    second_site, searchstrs, our_title = recorder.check_existing_group_calls[0]
    assert second_site.site_code == "FAKE2"
    assert searchstrs == new_searchstrs
    assert our_title == "New Title"


def test_a_caller_supplied_group_id_still_reaches_the_post_review_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the caller already knows the group (group_id set), the pre-review dupe search never runs
    (searchstrs stays whatever the caller passed, here None) but the post-review checks must still
    use search strings built from the edited metadata, not crash on the caller's None."""
    rls_data = {
        "format": "FLAC",
        "encoding": "Lossless",
        "artists": [("Artist", "main")],
        "title": "Old Title",
        "catno": None,
    }
    metadata = {**rls_data, "title": "New Title", "cover": None}
    recorder = Recorder()
    _install(monkeypatch, rls_data, metadata, recorder)
    new_searchstrs = generate_dupe_check_searchstrs(metadata["artists"], metadata["title"], metadata["catno"])

    site = FakeSite("FAKE1")
    anyio.run(salmon.uploader.upload, _as_gazelle_api(site), "/release", 5, "WEB", None, (), None)

    # group_id was already set, so recheck_dupe never runs.
    assert recorder.recheck_dupe_calls == []
    assert len(recorder.last_min_dupe_check_calls) == 1
    _, searchstrs, our_title = recorder.last_min_dupe_check_calls[0]
    assert searchstrs == new_searchstrs
    assert our_title == "New Title"
