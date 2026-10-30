"""The converted questions in salmon web, through their real call sites (ADR 0006, #760).

For each site: what each default it can give preselects, and that the picks the browser sends reach the site's own
parser as the line a terminal user would type. The sites get the in-memory fakes of test_interaction_typed_sites:
nothing reaches a tracker.
"""

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import anyio
import asyncclick as click
import pytest
from test_interaction_typed_sites import (  # pyright: ignore[reportMissingImports]
    CONVERSIONS,
    REQUESTS,
    SITE,
    SPECTRAL_IDS,
    confirm_group,
    conversions,
    edit_metadata,
    edit_metadata_stubs,
    hold_the_edition,
    request_site,
    review_metadata,
    spectral_ids,
)
from test_webui_interaction import Browser  # pyright: ignore[reportMissingImports]

from salmon import cfg, interaction, trackers
from salmon.config.validations import CONVERSION_KINDS
from salmon.errors import AbortAndDeleteFolder
from salmon.uploader import request_checker, spectrals
from salmon.webui.interaction import Picked, WebInteraction


@pytest.fixture(autouse=True)
def _interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.image, "default_spectral_ids", None)


def web(site: Callable[[], Awaitable[Any]], answers: list[Any]) -> tuple[Any, Browser]:
    """Run `site` as a salmon web job would, the browser answering `answers`: its result (or the abort raised)."""
    browser = Browser(answers)

    async def run() -> Any:
        with interaction.using(WebInteraction(browser)):
            try:
                return await site()
            except (click.Abort, AbortAndDeleteFolder) as e:
                return type(e)

    return anyio.run(run), browser


def picks(*keys: str) -> Picked:
    return Picked(picks=list(keys))


def sel(*keys: str, texts: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"picks": list(keys), "texts": list(texts), "mark": None}


# #8 upload to this group


@pytest.mark.parametrize(("held", "preselected"), [(False, sel("y")), (True, sel("a"))])
def test_upload_to_this_group_preselects_its_default(
    monkeypatch: pytest.MonkeyPatch, held: bool, preselected: dict[str, Any]
) -> None:
    if held:
        hold_the_edition(monkeypatch)
    result, browser = web(confirm_group(), [picks("n")])
    assert result is False
    question = browser.questions[0]
    assert question["preselected"] == preselected
    assert [(o["key"], o["action"], o["danger"]) for o in question["options"]["items"]] == [
        ("y", False, False),
        ("n", False, False),
        ("a", True, False),
        ("d", True, True),
    ]


@pytest.mark.parametrize(
    ("key", "outcome"), [("y", True), ("n", False), ("a", click.Abort), ("d", AbortAndDeleteFolder)]
)
def test_each_pick_of_upload_to_this_group_reaches_the_site(key: str, outcome: Any) -> None:
    result, browser = web(confirm_group(), [picks(key)])
    assert result == outcome
    assert len(browser.questions) == 1


# #10 lossy master


@pytest.mark.parametrize(("marks_found", "preselected"), [(False, sel("n")), (True, sel("y"))])
def test_lossy_master_preselects_its_default(marks_found: bool, preselected: dict[str, Any]) -> None:
    result, browser = web(partial(spectrals.prompt_lossy_master, False, True, marks_found), [picks("r")])
    assert result is None
    assert browser.questions[0]["preselected"] == preselected


@pytest.mark.parametrize(
    ("key", "outcome"), [("y", True), ("n", False), ("r", None), ("a", click.Abort), ("d", AbortAndDeleteFolder)]
)
def test_each_pick_of_lossy_master_reaches_the_site(key: str, outcome: Any) -> None:
    result, browser = web(partial(spectrals.prompt_lossy_master, False, True, False), [picks(key)])
    assert result == outcome
    assert len(browser.questions) == 1


# #11 spectral IDs


@pytest.mark.parametrize(
    ("configured", "lossy_master", "preselected"),
    [
        (None, False, sel("+")),
        (None, True, sel("*")),
        ("0", False, sel("0")),
        ("0", True, sel("*")),
        ("*", False, sel("*")),
        ("+", True, sel("+")),
        ("1 3", False, sel("1", "3")),
        ("3 9", False, sel("3")),
        ("9", False, sel("+")),
    ],
)
def test_spectral_ids_preselects_each_default(
    monkeypatch: pytest.MonkeyPatch, configured: str | None, lossy_master: bool, preselected: dict[str, Any]
) -> None:
    monkeypatch.setattr(cfg.image, "default_spectral_ids", configured)
    _result, browser = web(spectral_ids(lossy_master), [picks("0")])
    assert browser.questions[0]["preselected"] == preselected


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (picks("2"), {2: "Song.flac"}),
        (picks("3", "1"), {1: "Intro.flac", 3: "Outro.flac"}),
        (picks("*"), SPECTRAL_IDS),
        (picks("0"), None),
    ],
)
def test_each_pick_of_spectral_ids_reaches_the_site(answer: Picked, outcome: Any) -> None:
    result, browser = web(spectral_ids(), [answer])
    assert result == outcome
    assert len(browser.questions) == 1


def test_random_third_reaches_the_site() -> None:
    result, _browser = web(spectral_ids(), [picks("+")])
    assert len(result) == 1 and result.items() <= SPECTRAL_IDS.items()


# #14 fields to edit


def test_fields_to_edit_preselects_nothing_and_each_pick_reaches_its_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    site, edited = review_metadata(monkeypatch)
    for key in ("a", "l", "t", "g", "r", "y", "e", "c", "k", "u"):
        edited.clear()
        _result, browser = web(site, [picks(key), picks("n")])
        assert edited == [key]
        assert browser.questions[0]["preselected"] == sel()
        assert [q["error"] for q in browser.questions] == [None, None]


# #26 upload, or re-run the metadata step


def test_upload_or_rerun_names_its_buttons(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    reviews = edit_metadata_stubs(monkeypatch)
    result, browser = web(edit_metadata(str(tmp_path)), [False, True])
    assert result[0] == str(tmp_path)
    assert reviews == ["reviewed", "reviewed"]
    assert [(q["kind"], q["default"], q["labels"]) for q in browser.questions] == [
        ("confirm", True, ["Upload", "Re-run the metadata step"])
    ] * 2


# #31 fill a request


def test_fill_a_request_preselects_dont_fill_and_shows_the_requests() -> None:
    result, browser = web(partial(request_checker._prompt_for_request_id, SITE, REQUESTS), [picks("n")])
    assert result is None
    question = browser.questions[0]
    assert question["preselected"] == sel("n")
    first = question["options"]["items"][0]
    assert (first["group"], first["card"]["link"]) == (
        "Requests on RED",
        "https://red.test/requests.php?action=view&id=11",
    )
    assert question["options"]["free_text"] == {"placeholder": "Request URL or ID", "ids": True}


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (picks("1"), 11),
        (picks("3"), 13),
        (picks("n"), None),
        (Picked(texts=["https://red.test/requests.php?action=view&id=42"]), 42),
        (Picked(texts=["4"]), 4),
    ],
)
def test_each_answer_to_fill_a_request_reaches_the_site(answer: Picked, outcome: Any) -> None:
    result, browser = web(partial(request_checker._prompt_for_request_id, SITE, REQUESTS), [answer])
    assert result == outcome
    assert len(browser.questions) == 1


def test_a_row_number_typed_as_an_id_is_refused() -> None:
    # 2 is the row the CLI skipped: as a text it would read as that row, not as request 2.
    site = partial(request_checker._prompt_for_request_id, SITE, REQUESTS)
    result, browser = web(site, [Picked(texts=["3"]), Picked(texts=["2"]), picks("n")])
    assert result is None
    assert [q["error"] for q in browser.questions] == [
        None,
        "Error: 3 is result 3 above: pick it there, or paste the URL.",
        "Error: An ID is a number above 3: paste the URL, or pick from the options.",
    ]


# #31b are you sure


@pytest.mark.parametrize(("key", "outcome"), [("y", True), ("n", False)])
def test_fill_this_request_preselects_yes_and_each_pick_reaches_the_site(key: str, outcome: bool) -> None:
    result, browser = web(partial(request_checker._confirm_request_id, request_site(), 11), [picks(key)])
    assert result is outcome
    assert browser.questions[0]["preselected"] == sel("y")


# #35 formats to convert


@pytest.mark.parametrize(
    ("held", "wanted", "preselected"),
    [
        (frozenset(), None, sel("*")),
        (frozenset({"MP3 320"}), None, sel("1", "3")),
        (frozenset(), ("V0",), sel("3")),
        (frozenset(), (), sel("0")),
    ],
)
def test_formats_to_convert_preselects_each_default(
    monkeypatch: pytest.MonkeyPatch, held: frozenset[str], wanted: tuple[str, ...] | None, preselected: dict[str, Any]
) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", list(CONVERSION_KINDS))
    _result, browser = web(conversions(held, wanted), [picks("0")])
    assert browser.questions[0]["preselected"] == preselected


@pytest.mark.parametrize(
    ("answers", "outcome"),
    [
        ([picks("*")], CONVERSIONS),
        ([picks("0")], []),
        ([picks("3", "1"), True], ["16bit 48.0 kHz", "MP3 V0"]),
    ],
)
def test_each_pick_of_formats_to_convert_reaches_the_site(
    monkeypatch: pytest.MonkeyPatch, answers: list[Any], outcome: list[str]
) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", list(CONVERSION_KINDS))
    result, browser = web(conversions(), answers)
    assert result == outcome
    # A pick by number is confirmed by a plain yes or no, as on master.
    assert [("options" in q, "labels" in q) for q in browser.questions[1:]] == [(False, False)] * (len(answers) - 1)


# #36 (and #1) the next tracker


@pytest.mark.parametrize(("key", "outcome"), [("OPS", "OPS"), ("DIC", "DIC"), ("n", None)])
def test_tracker_choice_preselects_the_first_and_each_pick_reaches_the_site(key: str, outcome: str | None) -> None:
    result, browser = web(partial(trackers.choose_tracker, ["OPS", "DIC"]), [picks(key)])
    assert result == outcome
    assert browser.questions[0]["preselected"] == sel("OPS")
