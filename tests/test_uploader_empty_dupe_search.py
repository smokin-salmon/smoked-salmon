"""A title with no search string left (#931): no empty search goes out, and the group question is asked without one.

A release searched by its title alone ("Various Artists", or four or more main artists) whose title cleans to nothing
("Untitled", "(Remastered Edition)") gave one empty search string. It covered every other string, so a catalogue
number search went too, and the tracker's answer to an empty search was shown at the group prompt.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import asyncclick as click
import pytest
from helpers.uploader_dry_run import _album, _returning_async, _run_up

import salmon.uploader
from salmon import cfg, interaction
from salmon.uploader import dupe_checker, metadata_review, recheck_dupe, request_checker
from salmon.uploader.dupe_checker import check_existing_group, generate_dupe_check_searchstrs
from salmon.uploader.record import recording

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

VA = [("Various Artists", "main")]
# A title that cleans to nothing.
EMPTY = "(Remastered Edition)"
NO_SEARCH = "No dupe search is possible for this title: check the tracker yourself."


class Site:
    """A tracker that fails any request: nothing here may reach one."""

    site_code = "RED"
    site_string = "RED"
    base_url = "http://red.test"
    has_session_cookie = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def api_call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        raise AssertionError(f"sent {action}")

    async def get_uploads_from_log(self) -> list[tuple]:
        self.calls.append(("log", None))
        raise AssertionError("read the site log")


def _site(site: Site) -> "BaseGazelleApi":
    return cast("BaseGazelleApi", cast("object", site))


def _answering(monkeypatch: pytest.MonkeyPatch, answer: str) -> list[str]:
    asked: list[str] = []

    async def prompt(text: str, **_kwargs: Any) -> str:
        asked.append(click.unstyle(str(text)))
        return answer

    monkeypatch.setattr(interaction, "prompt", prompt)
    return asked


# --- The search strings --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("artists", "title", "catno", "expected"),
    [
        # The catalogue number search an "Untitled" title gets, with no number: the title search survives it.
        (VA, "Untitled", None, ["untitled"]),
        (VA, "Untitled EP", None, ["untitled"]),
        ([(f"Artist {n}", "main") for n in "ABCD"], "Untitled Vol. 2", None, ["untitled vol. 2", "untitled volume 2"]),
        ([], "Untitled", None, ["untitled"]),
        # A title that cleans to nothing: no search at all.
        (VA, "(Remastered Edition)", None, []),
        (VA, "(Remastered Edition)", "AP-001", []),
        (VA, "", None, []),
        # Punctuation that cleans to a space is no search string either.
        (VA, "Untitled", "-", ["untitled"]),
        (VA, "-", None, []),
        (VA, "/", None, []),
    ],
)
def test_no_empty_search_string(artists, title, catno, expected) -> None:
    assert generate_dupe_check_searchstrs(artists, title, catno) == expected


@pytest.mark.parametrize(
    ("artists", "title", "catno", "expected"),
    [
        ([("Artist", "main")], "Album", None, ["artist album"]),
        ([("Artist", "main")], "Album", "AP-001", ["artist album"]),
        (VA, "Album", None, ["album"]),
        (VA, "Untitled", "AP-001", ["ap 001", "untitled"]),
        ([("Artist", "main")], "Untitled", None, ["artist "]),
        ([("Artist", "main")], "Untitled", "AP-001", ["artist ap 001", "artist untitled"]),
        ([("Artist", "main")], "(Remastered Edition)", None, ["artist "]),
        ([("Artist", "main")], "AP-001", "AP-001", ["artist ap 001", "artist untitled"]),
    ],
)
def test_search_strings_as_before(artists, title, catno, expected) -> None:
    assert generate_dupe_check_searchstrs(artists, title, catno) == expected


# --- The group question ----------------------------------------------------------------------------------------------


def test_the_group_question_without_a_search(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Nothing is sent, the site log included, the run says so, and the question is asked as usual."""
    monkeypatch.setattr(cfg.upload.requests, "check_recent_uploads", True)
    monkeypatch.setattr(dupe_checker, "can_check_site_log", lambda _site: True)
    asked = _answering(monkeypatch, "n")
    site = Site()

    group_id = anyio.run(check_existing_group, _site(site), [])

    assert group_id is None
    assert site.calls == []
    out = capsys.readouterr().out
    assert NO_SEARCH in out
    assert "No groups were found" not in out
    assert len(asked) == 1
    assert "Would you like to upload to an existing group?" in asked[0]


def test_the_group_question_without_a_search_takes_a_group_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def confirm_group_id(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(dupe_checker, "_confirm_group_id", confirm_group_id)
    _answering(monkeypatch, "7")

    group_id = anyio.run(check_existing_group, _site(Site()), [])

    assert group_id == 7


def test_recheck_without_a_search_before_or_after_the_review_asks(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A Do-Not-Upload list kept the first search from running, and the reviewed title gives no search string."""
    asked = _answering(monkeypatch, "n")
    site = Site()

    group_id = anyio.run(recheck_dupe, _site(site), [], {"artists": VA, "title": EMPTY, "catno": None})

    assert group_id is None
    assert site.calls == []
    out = capsys.readouterr().out
    assert NO_SEARCH in out
    assert len(asked) == 1


def test_recheck_without_a_search_stops_under_yyy(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    asked = _answering(monkeypatch, "n")
    site = Site()

    with interaction.assuming_defaults(), pytest.raises(click.exceptions.Exit) as stopped:
        anyio.run(recheck_dupe, _site(site), [], {"artists": VA, "title": EMPTY, "catno": None})

    assert stopped.value.exit_code == 1
    assert site.calls == []
    assert asked == []
    out = capsys.readouterr().out
    assert "--group-id" in out


def test_recheck_after_a_search_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first search ran: a review that leaves no search string keeps what it found, -yyy or not."""
    asked = _answering(monkeypatch, "n")
    site = Site()

    with interaction.assuming_defaults():
        group_id = anyio.run(recheck_dupe, _site(site), ["album"], {"artists": VA, "title": EMPTY, "catno": None})

    assert group_id is None
    assert site.calls == []
    assert asked == []


# --- Requests and the last-minute check ------------------------------------------------------------------------------


def test_no_request_search_without_a_search_string(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(cfg.upload.requests, "always_ask_for_request_fill", False)
    site = Site()

    request_id = anyio.run(request_checker.check_requests, _site(site), [])

    assert request_id is None
    assert site.calls == []
    out = capsys.readouterr().out
    assert "No requests were found" not in out


def test_no_last_minute_log_read_without_a_search_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata_review, "can_check_site_log", lambda _site: True)
    site = Site()

    anyio.run(salmon.uploader.last_min_dupe_check, _site(site), [], EMPTY)

    assert site.calls == []


def test_the_request_question_without_a_search_string_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """With always_ask_for_request_fill, the request question is asked with nothing searched, as before."""
    monkeypatch.setattr(cfg.upload.requests, "always_ask_for_request_fill", True)
    asked: list[bool] = []

    async def prompt_for_request_id(_site, results):
        asked.append(results == [])
        return None

    monkeypatch.setattr(request_checker, "_prompt_for_request_id", prompt_for_request_id)
    site = Site()

    request_id = anyio.run(request_checker.check_requests, _site(site), [])

    assert request_id is None
    assert site.calls == []
    assert asked == [True]


# --- salmon up on the fake tracker -----------------------------------------------------------------------------------


def _release(artists, title: str, catno: str | None) -> dict[str, Any]:
    return {"format": "FLAC", "encoding": "Lossless", "artists": artists, "title": title, "catno": catno}


def _metadata(release: dict[str, Any]) -> dict[str, Any]:
    return {
        **release,
        "source": "WEB",
        # Not the edition the fake tracker's group holds, so -g has no dupe to stop at.
        "year": 2021,
        "group_year": 2021,
        "date": "2021-01-01",
        "label": "Other Label",
        "rls_type": "Album",
        "edition_title": None,
        "encoding_vbr": False,
        "scene": False,
        "cover": None,
        "comment": None,
        "urls": [],
        "genres": ["Electronic"],
    }


def _up(
    monkeypatch, tmp_path: Path, release: dict[str, Any], args=(), *, yes_all=True, input="\n" * 20, trackers=("RED",)
):
    torrents = tmp_path / "torrents"
    torrents.mkdir()
    (tmp_path / "downloads").mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path / "downloads"))
    metadata = _metadata(release)
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        ("--conversions", "none", *args),
        input=input,
        multi_tracker_upload=False,
        yes_all=yes_all,
        trackers=trackers,
        construct_rls_data=_returning_async(release),
        get_metadata=_returning_async((metadata, None)),
        review_metadata_with_ai=_returning_async(metadata),
        check_spectrals=_returning_async((False, {})),
        resolve_cover_url=_returning_async((True, "https://images.test/cover.jpg")),
    )
    searches = [
        (sent.query["action"], sent.query.get("searchstr", sent.query.get("search")))
        for sent in run.tracker.sent
        if sent.query.get("action") in ("browse", "requests")
    ]
    uploads = [sent for sent in run.tracker.sent if sent.query.get("action") == "upload"]
    return run.result, sorted(searches), len(uploads)


def test_up_empty_title_stops_under_yyy(monkeypatch, tmp_path) -> None:
    result, searches, uploads = _up(monkeypatch, tmp_path, _release(VA, EMPTY, None))

    assert result.exit_code == 1, result.output
    assert searches == []
    assert uploads == 0
    assert "--group-id" in result.output


def test_up_empty_title_with_a_group_id_under_yyy(monkeypatch, tmp_path) -> None:
    result, searches, uploads = _up(monkeypatch, tmp_path, _release(VA, EMPTY, None), ("-g", "7"))

    assert result.exit_code == 0, result.output
    assert searches == []
    assert uploads == 1, result.output


def test_up_empty_title_next_tracker_under_yyy(monkeypatch, tmp_path) -> None:
    """RED's Do-Not-Upload list refuses the release: OPS gets no search either, and -yyy uploads nowhere."""
    listed = salmon.uploader._do_not_upload_refusal
    monkeypatch.setattr(
        salmon.uploader,
        "_do_not_upload_refusal",
        lambda tracker, release, said=None: "listed" if tracker == "RED" else listed(tracker, release, said),
    )

    with recording() as run:
        result, searches, uploads = _up(monkeypatch, tmp_path, _release(VA, EMPTY, None), trackers=("RED", "OPS"))

    assert searches == []
    assert uploads == 0, result.output
    assert "Not uploading to OPS" in result.output
    assert run.exit_status(result.exit_code) == 1


def test_up_empty_title_with_a_group_id_skips_the_next_tracker_under_yyy(monkeypatch, tmp_path) -> None:
    """-g names RED's group: OPS has no search, so -yyy skips it, and the run says something may be up (#949)."""
    with recording() as run:
        result, searches, uploads = _up(
            monkeypatch, tmp_path, _release(VA, EMPTY, None), ("-g", "7"), trackers=("RED", "OPS")
        )

    assert searches == []
    assert uploads == 1, result.output
    assert "Not uploading to OPS" in result.output
    assert run.exit_status(result.exit_code) == 3


def test_up_empty_title_asks_without_yyy(monkeypatch, tmp_path) -> None:
    result, searches, uploads = _up(
        monkeypatch, tmp_path, _release(VA, EMPTY, None), yes_all=False, input="n\n" + "\n" * 20
    )

    assert result.exit_code == 0, result.output
    assert searches == []
    assert uploads == 1, result.output
    assert NO_SEARCH in result.output


def test_up_untitled_searches_its_title(monkeypatch, tmp_path) -> None:
    result, searches, uploads = _up(monkeypatch, tmp_path, _release(VA, "Untitled", None))

    assert result.exit_code == 0, result.output
    assert searches == [("browse", "untitled"), ("requests", "untitled")]
    assert uploads == 1, result.output


@pytest.mark.parametrize(
    ("artists", "title", "catno", "searched"),
    [
        ([("Artist", "main")], "Album", "CAT1", ["artist album"]),
        (VA, "Untitled", "AP-001", ["ap 001", "untitled"]),
    ],
)
def test_up_as_before(monkeypatch, tmp_path, artists, title, catno, searched) -> None:
    result, searches, uploads = _up(monkeypatch, tmp_path, _release(artists, title, catno))

    assert result.exit_code == 0, result.output
    assert searches == sorted([*(("browse", s) for s in searched), *(("requests", s) for s in searched)])
    assert uploads == 1, result.output
