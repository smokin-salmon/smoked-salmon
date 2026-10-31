"""[b]ack to the metadata results at salmon up's review (#802): the same results again with no second search, and
nothing written to the files before the new pick is reviewed.

The runs go against the local fake tracker of test_uploader_dry_run and a fake image host, never a real one.
"""

from copy import deepcopy
from functools import partial
from typing import Any

import pytest
from test_interaction_typed_sites import ask, edit_metadata_stubs  # pyright: ignore[reportMissingImports]
from test_tagger_ai_review import make_metadata, make_review  # pyright: ignore[reportMissingImports]
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    SOURCE_URL,
    _album,
    _retag,
    _returning_async,
    _run_up,
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

import salmon.uploader as uploader
from salmon.errors import PickOtherResults
from salmon.search.base import IdentData
from salmon.tagger import ai_review, review
from salmon.tagger import metadata as metadata_mod

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

FIRST_URL = "https://www.deezer.com/album/111"
SECOND_URL = "https://www.deezer.com/album/222"
TAGS_ARTISTS = [("Artist", "main")]
# What a pick adds to the release, as the stubbed metadata of test_uploader_dry_run has it.
PICKED = {
    "group_year": 2020,
    "date": "2020-01-01",
    "label": "Label",
    "rls_type": "Album",
    "edition_title": None,
    "encoding_vbr": False,
    "scene": False,
    "cover": None,
    "comment": None,
    "urls": [],
    "genres": ["Electronic"],
}


def _tags_release() -> dict[str, Any]:
    """The release as the files' tags give it, before any pick."""
    return {
        "format": "FLAC",
        "encoding": "Lossless",
        "artists": deepcopy(TAGS_ARTISTS),
        "title": "Album",
        "catno": "CAT1",
        "source": "WEB",
        "year": 2020,
        "group_year": 2020,
        "edition_title": None,
        "label": None,
        "upc": None,
        "genres": [],
        "rls_type": None,
        "comment": None,
        "urls": [],
        "tracks": {},
    }


def _up(monkeypatch, tmp_path, picks: list[tuple[str, str | None]], **fakes: Any) -> tuple[Any, list[tuple]]:
    """salmon up whose search finds two results, the i-th pick giving the title and starred URL of picks[i].

    The review goes back from the pick titled "First" when it may. Returns the run and what happened, in order.
    """
    events: list[tuple] = []

    async def search(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        events.append(("search",))
        return {
            "Deezer": {
                "111": (IdentData("Artist", "Album", 2020, None, "WEB"), "Artist - Album"),
                "222": (IdentData("Artist", "Other", 2020, None, "WEB"), "Artist - Other"),
            }
        }

    async def select(_choices: Any, rls_data: dict[str, Any], default: str | None = None) -> tuple[dict, str | None]:
        title, url = picks[sum(event[0] == "pick" for event in events)]
        events.append(("pick", default, deepcopy(rls_data["artists"]), rls_data["title"]))
        # As combine_metadatas and the artist corrections do: the pick is written into rls_data, nested values too.
        rls_data["title"] = title
        rls_data["artists"][0] = ("Corrected Artist", "main")
        return {**rls_data, **PICKED}, url

    async def finish(metadata: dict[str, Any], _path: str) -> dict[str, Any]:
        return metadata

    async def review(
        metadata: dict[str, Any], _rls_data: Any, source_url: str | None, _validator: Any, manual_review, **_kwargs: Any
    ):
        offered = manual_review.keywords["picked"] is not None
        events.append(("review", metadata["title"], source_url, offered))
        if metadata["title"] == "First" and offered:
            raise PickOtherResults
        return metadata

    async def tag_files(path: str, *args: Any, **_kwargs: Any) -> None:
        events.append(("tag",))
        await _retag(path, *args)

    recheck_dupe = uploader.recheck_dupe
    prepare_and_upload = uploader.prepare_and_upload

    async def recording_recheck_dupe(site: Any, searchstrs: Any, metadata: dict[str, Any]) -> Any:
        events.append(("dupe check", metadata["title"]))
        return await recheck_dupe(site, searchstrs, metadata)

    async def recording_upload(**kwargs: Any) -> Any:
        events.append(("upload", kwargs["source_url"]))
        return await prepare_and_upload(**kwargs)

    monkeypatch.setattr(metadata_mod, "run_metasearch", search)
    monkeypatch.setattr(metadata_mod, "_select_choice", select)
    monkeypatch.setattr(metadata_mod, "_finish_metadata", finish)
    torrents = tmp_path / "torrents"
    torrents.mkdir()
    monkeypatch.setattr(uploader.cfg.directory, "download_directory", str(tmp_path))
    monkeypatch.setattr(uploader.cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(uploader.cfg.directory, "library_dirs", [])
    run = _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        # A new group, at the group question and at the dupe check after the review.
        input="\n\n",
        multi_tracker_upload=False,
        construct_rls_data=_returning_async(_tags_release()),
        check_spectrals=_returning_async((False, None)),
        get_metadata=metadata_mod.get_metadata,
        review_metadata_with_ai=review,
        tag_files=tag_files,
        recheck_dupe=recording_recheck_dupe,
        prepare_and_upload=recording_upload,
        **fakes,
    )
    return run, events


@pytest.mark.parametrize(
    ("second_url", "source_url"), [(SECOND_URL, SECOND_URL), (None, SOURCE_URL)], ids=["starred", "not-starred"]
)
def test_going_back_picks_again_from_the_same_results_before_anything_is_written(
    monkeypatch, tmp_path, second_url: str | None, source_url: str
) -> None:
    run, events = _up(monkeypatch, tmp_path, [("First", FIRST_URL), ("Second", second_url)])

    assert run.result.exit_code == 0, run.result.output
    default = events[1][1]
    assert default is not None
    expected = [
        ("search",),
        ("pick", default, TAGS_ARTISTS, "Album"),
        ("review", "First", FIRST_URL, True),
        # The same results and pre-typed answer, from the release as it was before the first pick.
        ("pick", default, TAGS_ARTISTS, "Album"),
        # The AI review runs again, for the new pick and with its source URL: a pick with no starred URL keeps the
        # run's --source-url, not the abandoned pick's.
        ("review", "Second", source_url, True),
        ("tag",),
        ("dupe check", "Second"),
        ("upload", source_url),
    ]
    assert events[: len(expected)] == expected
    assert {event for event in events if event[0] == "upload"} == {("upload", source_url)}


def test_skip_search_that_took_its_url_offers_no_way_back(monkeypatch, tmp_path) -> None:
    async def scraped(url: str, rls_data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        return {**rls_data, **PICKED, "title": "First"}, url

    monkeypatch.setattr(metadata_mod, "_metadata_from_source_url", scraped)
    run, events = _up(monkeypatch, tmp_path, [], args=("--skip-search",))

    assert run.result.exit_code == 0, run.result.output
    assert ("search",) not in events
    assert [event for event in events if event[0] == "review"] == [("review", "First", SOURCE_URL, False)]


def test_after_rerunning_the_metadata_step_there_is_no_way_back(monkeypatch, tmp_path) -> None:
    # The files now carry the first pick: going back would leave its album tags in them.
    edit_metadata_stubs(monkeypatch)
    offers: list[bool] = []

    async def review(
        metadata: dict[str, Any], _rls_data: Any, _source_url: Any, _validator: Any, manual_review, **_kwargs: Any
    ):
        offers.append(manual_review.keywords["picked"] is not None)
        return metadata

    monkeypatch.setattr(uploader, "review_metadata_with_ai", review)
    site = partial(
        uploader.edit_metadata,
        str(tmp_path),
        {},
        {"scene": False, "genres": ["Pop"]},
        None,
        "WEB",
        {},
        False,
        False,
        None,
        skip_integrity_check=True,
        offer_other_results=True,
    )

    # Re-run the metadata step, then upload.
    ask(monkeypatch, site, [False, True])

    assert offers == [True, False]


def test_going_back_after_the_ai_review_applied_its_changes_asks_first(monkeypatch, tmp_path) -> None:
    # The review the AI's changes reopen starts from them: going back must still count them as lost.
    edit_metadata_stubs(monkeypatch)
    asked_ai: list[str] = []

    async def request_ai_review(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], str]:
        asked_ai.append("ai")
        return make_review(year="2003"), "response-1"

    monkeypatch.setattr(uploader, "review_metadata_with_ai", ai_review.review_metadata_with_ai)
    monkeypatch.setattr(uploader, "metadata_validator", lambda _metadata: None)
    monkeypatch.setattr(review, "_print_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_review, "_request_ai_review", request_ai_review)
    monkeypatch.setattr(uploader.cfg.upload.ai_review, "enabled", True)
    site = partial(
        uploader.edit_metadata,
        str(tmp_path),
        {},
        {**make_metadata(), "scene": False},
        None,
        "WEB",
        {},
        False,
        False,
        None,
        skip_integrity_check=True,
        offer_other_results=True,
    )

    # Nothing at the first review, run the AI and apply its change, then b at the review that follows: asked.
    result, terminal, _described = ask(monkeypatch, site, ["n", True, "a", "b", True])

    assert result is PickOtherResults
    assert asked_ai == ["ai"]
    assert [call[0] for call in terminal.calls] == ["prompt", "confirm", "prompt", "prompt", "confirm"]
    assert "Going back drops the changes made since the pick." in terminal.calls[-1][1]
