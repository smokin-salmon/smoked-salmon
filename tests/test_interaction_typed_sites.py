"""The questions that describe their answers for salmon web ask the terminal exactly as before (ADR 0006, #760).

For each converted call site: the text and default asyncclick is given are master's, copied here; every key its
description lists, typed in the terminal, is accepted without the question coming back; and where the site has a
`-yyy` branch, asyncclick is not called and the result is master's. No tracker: the sites get fakes that answer from
memory.
"""

from functools import partial
from typing import Any

import asyncclick as click
import pytest
from helpers.interaction_typed_sites import (
    CONVERSIONS,
    FIELDS,
    GROUP,
    REQUESTS,
    SEARCH_RESULTS,
    SITE,
    SPECTRAL_IDS,
    ask,
    confirm_group,
    conversions,
    edit_metadata,
    edit_metadata_stubs,
    hold_the_edition,
    keys,
    prompt_call,
    recent_group,
    request_site,
    review_metadata,
    search_group,
    spectral_ids,
)

import salmon.tagger.review as review
from salmon import cfg, interaction, trackers
from salmon.config.validations import CONVERSION_KINDS
from salmon.errors import AbortAndDeleteFolder, PickOtherResults, SkipTracker
from salmon.uploader import dupe_checker, request_checker, spectrals


@pytest.fixture(autouse=True)
def _interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.image, "default_spectral_ids", None)


# --- No existing test changed: see the PR. Each site's text, default and keys follow. ---------------------------------


# #8 dupe_checker._confirm_group_id: upload to this group


@pytest.mark.parametrize(
    ("offer_deletion", "held", "text", "default"),
    [
        (
            True,
            False,
            "\nAre you sure you would you like to upload this torrent to this group? [Y]es, [n]ew group, [a]bort, "
            "[d]elete music folder",
            "Y",
        ),
        (
            False,
            True,
            "\nAre you sure you would you like to upload this torrent to this group? [Y]es, [n]ew group, [a]bort",
            "a",
        ),
    ],
)
def test_upload_to_this_group_asks_as_on_master(
    monkeypatch: pytest.MonkeyPatch, offer_deletion: bool, held: bool, text: str, default: str
) -> None:
    if held:
        hold_the_edition(monkeypatch)
    result, terminal, _described = ask(monkeypatch, confirm_group(offer_deletion), ["y"])
    assert result is True
    assert terminal.calls == [prompt_call(text, default)]


@pytest.mark.parametrize("offer_deletion", [True, False])
def test_every_key_of_upload_to_this_group_is_accepted(monkeypatch: pytest.MonkeyPatch, offer_deletion: bool) -> None:
    _result, _terminal, described = ask(monkeypatch, confirm_group(offer_deletion), ["y"])
    expected = {"y": True, "n": False, "a": click.Abort} | ({"d": AbortAndDeleteFolder} if offer_deletion else {})
    assert keys(described.options[0]) == [
        ("y", "Upload to this group"),
        ("n", "New group"),
        ("a", "Abort"),
        *([("d", "Delete music folder")] if offer_deletion else []),
    ]
    options = described.options[0]
    assert options is not None
    assert [(o.action, o.danger) for o in options.items] == [(False, False), (False, False), (True, False)] + (
        [(True, True)] if offer_deletion else []
    )
    for key, outcome in expected.items():
        result, terminal, _described = ask(monkeypatch, confirm_group(offer_deletion), [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("offer_skip", [True, False])
def test_upload_to_this_group_offers_skip_this_tracker_only_when_the_cli_does(
    monkeypatch: pytest.MonkeyPatch, offer_skip: bool, held: bool
) -> None:
    if held:
        hold_the_edition(monkeypatch)
    text = (
        "\nAre you sure you would you like to upload this torrent to this group? [Y]es, [n]ew group, "
        f"{'[s]kip this tracker, ' if offer_skip else ''}[a]bort, [d]elete music folder"
    )
    for yyy in (False, True):
        # -yyy asks this question too, as on master.
        result, terminal, described = ask(monkeypatch, confirm_group(offer_skip=offer_skip), ["y"], yyy=yyy)
        assert result is True
        assert terminal.calls == [prompt_call(text, "a" if held else "Y")]
    assert keys(described.options[0]) == [
        ("y", "Upload to this group"),
        ("n", "New group"),
        *([("s", "Skip this tracker")] if offer_skip else []),
        ("a", "Abort"),
        ("d", "Delete music folder"),
    ]
    options = described.options[0]
    assert options is not None
    assert [(o.key, o.action, o.danger) for o in options.items if o.key == "s"] == (
        [("s", True, False)] if offer_skip else []
    )
    if offer_skip:
        result, terminal, _described = ask(monkeypatch, confirm_group(offer_skip=True), ["s"])
        assert result is SkipTracker
        assert len(terminal.calls) == 1


# #10 spectrals.prompt_lossy_master: lossy master


@pytest.mark.parametrize(
    ("marks_found", "offer_deletion", "text", "default"),
    [
        (
            False,
            True,
            "\nIs this release lossy mastered? [y]es, [N]o, [r]eopen spectrals, [a]bort, [d]elete music folder",
            "n",
        ),
        (True, False, "\nIs this release lossy mastered? [Y]es, [n]o, [r]eopen spectrals, [a]bort", "y"),
    ],
)
def test_lossy_master_asks_as_on_master(
    monkeypatch: pytest.MonkeyPatch, marks_found: bool, offer_deletion: bool, text: str, default: str
) -> None:
    site = partial(spectrals.prompt_lossy_master, False, offer_deletion, marks_found)
    result, terminal, _described = ask(monkeypatch, site, ["n"])
    assert result is False
    assert terminal.calls == [prompt_call(text, default, click.STRING)]


@pytest.mark.parametrize("offer_deletion", [True, False])
def test_every_key_of_lossy_master_is_accepted(monkeypatch: pytest.MonkeyPatch, offer_deletion: bool) -> None:
    site = partial(spectrals.prompt_lossy_master, False, offer_deletion, False)
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    assert keys(described.options[0]) == [
        ("y", "Lossy mastered"),
        ("n", "Not lossy mastered"),
        ("r", "Reopen spectrals"),
        ("a", "Abort"),
        *([("d", "Delete music folder")] if offer_deletion else []),
    ]
    expected = {"y": True, "n": False, "r": None, "a": click.Abort} | (
        {"d": AbortAndDeleteFolder} if offer_deletion else {}
    )
    for key, outcome in expected.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1


def test_lossy_master_with_yyy_is_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(spectrals.prompt_lossy_master, False, True, False)
    result, terminal, _described = ask(monkeypatch, site, [], yyy=True)
    assert result is False
    assert terminal.calls == []


# #11 spectrals.prompt_spectrals: spectral IDs

SPECTRALS_TEXT = (
    "What spectral IDs would you like to upload to catbox? "
    '(space-separated list of IDs, "0" for none, "*" for all, or "+" for a randomized selection)'
)


@pytest.mark.parametrize(("lossy_master", "default"), [(False, "+"), (True, "*")])
def test_spectral_ids_asks_as_on_master(monkeypatch: pytest.MonkeyPatch, lossy_master: bool, default: str) -> None:
    monkeypatch.setattr(cfg.image, "default_spectral_ids", None)
    result, terminal, _described = ask(monkeypatch, spectral_ids(lossy_master), ["2"])
    assert result == {2: "Song.flac"}
    assert terminal.calls == [prompt_call(SPECTRALS_TEXT, default)]


def test_every_key_of_spectral_ids_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _result, _terminal, described = ask(monkeypatch, spectral_ids(), ["1"])
    options = described.options[0]
    assert keys(options) == [
        ("1", "01 Intro.flac"),
        ("2", "02 Song.flac"),
        ("3", "03 Outro.flac"),
        ("*", "All"),
        ("0", "None"),
        ("+", "Random third"),
    ]
    assert options is not None and options.several
    assert [o.alone for o in options.items] == [False, False, False, True, True, True]
    for key, outcome in {"1": {1: "Intro.flac"}, "3": {3: "Outro.flac"}, "*": SPECTRAL_IDS, "0": None}.items():
        result, terminal, _described = ask(monkeypatch, spectral_ids(), [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1
    result, terminal, _described = ask(monkeypatch, spectral_ids(), ["+"])
    assert len(result) == 1 and result.items() <= SPECTRAL_IDS.items()
    assert len(terminal.calls) == 1
    # Several picks make one line, in the order of the options.
    result, terminal, _described = ask(monkeypatch, spectral_ids(), ["1 3"])
    assert result == {1: "Intro.flac", 3: "Outro.flac"}


@pytest.mark.parametrize(
    ("configured", "lossy_master", "outcome"),
    [(None, True, SPECTRAL_IDS), ("1 3", False, {1: "Intro.flac", 3: "Outro.flac"}), ("0", False, None)],
)
def test_spectral_ids_with_yyy_is_not_asked(
    monkeypatch: pytest.MonkeyPatch, configured: str | None, lossy_master: bool, outcome: Any
) -> None:
    monkeypatch.setattr(cfg.image, "default_spectral_ids", configured)
    result, terminal, _described = ask(monkeypatch, spectral_ids(lossy_master), [], yyy=True)
    assert result == outcome
    assert terminal.calls == []


# #14 review.review_metadata: fields to edit

FIELDS_TEXT = (
    "\nAre there any metadata fields you would like to edit? [a]rtists, "
    "artist a[L]iases (rename or split an artist on every track), [t]itle, [g]enres, "
    "[r]elease type, [y]ears, [e]dition info, [c]omment, trac[k]s, [u]rls, [n]othing"
)


def test_fields_to_edit_asks_as_on_master(monkeypatch: pytest.MonkeyPatch) -> None:
    site, edited = review_metadata(monkeypatch)
    _result, terminal, _described = ask(monkeypatch, site, ["n"])
    assert terminal.calls == [prompt_call(FIELDS_TEXT, None)]
    assert edited == []


def test_every_key_of_fields_to_edit_is_accepted(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    site, edited = review_metadata(monkeypatch)
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    assert keys(described.options[0]) == [
        ("a", "Artists"),
        ("l", "Artist aliases"),
        ("t", "Title"),
        ("g", "Genres"),
        ("r", "Release type"),
        ("y", "Years"),
        ("e", "Edition info"),
        ("c", "Comment"),
        ("k", "Tracks"),
        ("u", "URLs"),
        ("n", "Nothing"),
    ]
    for key in FIELDS:
        edited.clear()
        # A field's editor runs, then the review asks again, as it does after every edit; "n" ends it.
        _result, terminal, _described = ask(monkeypatch, site, [key, "n"])
        assert edited == [key]
        assert len(terminal.calls) == 2
    assert "is not a valid editing option" not in capsys.readouterr().out


def test_back_to_the_metadata_results_is_a_key_only_when_offered(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    offered, _edited = review_metadata(monkeypatch, offer_other_results=True)
    # With no edits made, going back asks nothing more.
    result, terminal, described = ask(monkeypatch, offered, ["b"])
    assert result is PickOtherResults
    assert terminal.calls == [prompt_call(FIELDS_TEXT + ", [b]ack to the metadata results", None)]
    assert keys(described.options[0])[-1] == ("b", "Back to the metadata results")
    # Without the offer, b is no answer, as on master.
    site, _edited = review_metadata(monkeypatch)
    result, terminal, _described = ask(monkeypatch, site, ["b", "n"])
    assert result is not PickOtherResults
    assert len(terminal.calls) == 2
    assert "b is not a valid editing option" in capsys.readouterr().out


def test_going_back_after_an_edit_asks_first_and_no_stays_at_the_review(monkeypatch: pytest.MonkeyPatch) -> None:
    site, _edited = review_metadata(monkeypatch, offer_other_results=True)

    async def edit_title(metadata: dict[str, Any], *_args: Any, **_kwargs: Any) -> None:
        metadata["title"] = "Edited"

    monkeypatch.setattr(review, "_edit_title", edit_title)
    # An edit, then b: asked, and no (the default) stays. b again: yes goes back.
    result, terminal, _described = ask(monkeypatch, site, ["t", "b", False, "b", True])
    assert result is PickOtherResults
    asked = click.style("Going back drops the changes made since the pick. Go back?", fg="magenta")
    assert [call[0] for call in terminal.calls] == ["prompt", "prompt", "confirm", "prompt", "confirm"]
    assert terminal.calls[2] == ("confirm", asked, {"default": False, "abort": False})


# #26 uploader.edit_metadata: upload, or re-run the metadata step


def test_upload_or_rerun_asks_as_on_master(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    reviews = edit_metadata_stubs(monkeypatch)
    result, terminal, described = ask(monkeypatch, edit_metadata(str(tmp_path)), [True])
    assert result[0] == str(tmp_path)
    assert terminal.calls == [
        (
            "confirm",
            click.style("\nWould you like to upload the torrent? (No to re-run metadata section)", fg="magenta"),
            {"default": True, "abort": False},
        )
    ]
    assert described.labels == [("Upload", "Re-run the metadata step")]
    assert reviews == ["reviewed"]


def test_both_answers_to_upload_or_rerun_are_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    reviews = edit_metadata_stubs(monkeypatch)
    # No re-runs the metadata step, then asks again.
    _result, terminal, _described = ask(monkeypatch, edit_metadata(str(tmp_path)), [False, True])
    assert len(terminal.calls) == 2
    assert reviews == ["reviewed", "reviewed"]


def test_upload_or_rerun_with_yyy_is_not_asked(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    reviews = edit_metadata_stubs(monkeypatch)
    result, terminal, _described = ask(monkeypatch, edit_metadata(str(tmp_path)), [], yyy=True)
    assert result[0] == str(tmp_path)
    assert result[1]["tags"] == "Pop"
    assert terminal.calls == []
    assert reviews == ["reviewed"]


# #31 request_checker._prompt_for_request_id: fill a request


def test_fill_a_request_asks_as_on_master(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._prompt_for_request_id, SITE, REQUESTS)
    result, terminal, _described = ask(monkeypatch, site, ["n"])
    assert result is None
    assert terminal.calls == [prompt_call("\nFill a request? Choose from results, paste a url, or do[n]t.", "N")]


def test_every_key_of_fill_a_request_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._prompt_for_request_id, SITE, REQUESTS)
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    options = described.options[0]
    # Every row, with the number the CLI reads as that row: the one printed without its year by number and link only.
    assert keys(options) == [
        ("1", "Artist - Album"),
        ("2", "Request 2"),
        ("3", "Various Artists - Compilation"),
        ("n", "Don't fill"),
    ]
    assert options is not None
    assert {option.group for option in options.items[:3]} == {"Requests on RED"}
    assert options.items[0].card == interaction.Card(
        (
            ("Year", "2020"),
            ("Release type", "Album"),
            ("Requirements", "Lossless or 24bit Lossless / FLAC / WEB or CD"),
            ("Bounty", "100 MiB"),
        ),
        link="https://red.test/requests.php?action=view&id=11",
    )
    assert options.items[1].card == interaction.Card(link="https://red.test/requests.php?action=view&id=12")
    assert options.items[2].card is not None
    assert [label for label, _value in options.items[2].card.fields] == ["Year", "Release type", "Requirements"]
    assert options.free_text == interaction.FreeText("Request URL or ID", ids=True)
    for key, outcome in {"1": 11, "2": 12, "3": 13, "n": None}.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1
    # What the web sends as a text: a request URL, or an ID above every numbered key.
    for text, outcome in {"https://red.test/requests.php?action=view&id=42": 42, "4": 4}.items():
        result, terminal, _described = ask(monkeypatch, site, [text])
        assert (text, result) == (text, outcome)
        assert len(terminal.calls) == 1


def test_a_request_row_without_an_id_is_still_listed_by_its_number(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._prompt_for_request_id, SITE, [*REQUESTS[:1], {"title": "Broken"}])
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    options = described.options[0]
    assert keys(options) == [("1", "Artist - Album"), ("2", "Request 2"), ("n", "Don't fill")]
    assert options is not None and options.items[1].card is None


def test_picking_a_request_row_without_an_id_asks_again(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    site = partial(request_checker._prompt_for_request_id, SITE, [*REQUESTS[:1], {"title": "Broken"}])
    result, terminal, _described = ask(monkeypatch, site, ["2", "1"])
    assert result == 11
    assert len(terminal.calls) == 2
    assert "Result 2 has no request ID: pick another, or paste a request URL." in capsys.readouterr().out


def test_fill_a_request_with_no_results_still_describes_its_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._prompt_for_request_id, SITE, [])
    result, _terminal, described = ask(monkeypatch, site, ["5"])
    assert result == 5
    assert keys(described.options[0]) == [("n", "Don't fill")]


# #31b request_checker._confirm_request_id: are you sure


def test_fill_this_request_asks_as_on_master(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._confirm_request_id, request_site(), 11)
    result, terminal, _described = ask(monkeypatch, site, ["y"])
    assert result is True
    assert terminal.calls == [prompt_call("\nAre you sure you would you like to fill this request [Y]es, [n]o", "Y")]


def test_every_key_of_fill_this_request_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._confirm_request_id, request_site(), 11)
    _result, _terminal, described = ask(monkeypatch, site, ["y"])
    assert keys(described.options[0]) == [("y", "Fill this request"), ("n", "Don't fill it")]
    for key, outcome in {"y": True, "n": False}.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1


def test_fill_this_request_with_yyy_is_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(request_checker._confirm_request_id, request_site(), 11)
    result, terminal, _described = ask(monkeypatch, site, [], yyy=True)
    assert result is True
    assert terminal.calls == []


# #35 uploader.prompt_downconversion_choice: formats to convert

CONVERSIONS_TEXT = '\nSelect formats to convert (space-separated list of IDs, "0" for none, "*" for all)'


@pytest.mark.parametrize(
    ("held", "wanted", "default"),
    [(frozenset(), None, "*"), (frozenset({"MP3 320"}), None, "1 3"), (frozenset(), (), "0")],
)
def test_formats_to_convert_asks_as_on_master(
    monkeypatch: pytest.MonkeyPatch, held: frozenset[str], wanted: tuple[str, ...] | None, default: str
) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", list(CONVERSION_KINDS))
    result, terminal, _described = ask(monkeypatch, conversions(held, wanted), ["0"])
    assert result == []
    assert terminal.calls == [prompt_call(CONVERSIONS_TEXT, default)]


def test_every_key_of_formats_to_convert_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", list(CONVERSION_KINDS))
    held = frozenset({"MP3 320"})
    _result, _terminal, described = ask(monkeypatch, conversions(held), ["0"])
    options = described.options[0]
    assert keys(options) == [
        ("1", "16bit 48.0 kHz"),
        ("2", "MP3 320"),
        ("3", "MP3 V0"),
        ("*", "All formats"),
        ("0", "None"),
    ]
    assert options is not None and options.several
    assert [o.card for o in options.items] == [
        None,
        interaction.Card(note="DUPE RISK: this edition already has it"),
        None,
        None,
        None,
    ]
    assert [o.alone for o in options.items] == [False, False, False, True, True]
    for key, outcome in {"*": CONVERSIONS, "0": []}.items():
        result, terminal, _described = ask(monkeypatch, conversions(held), [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1
    for key, outcome in {
        "1": ["16bit 48.0 kHz"],
        "2": ["MP3 320"],
        "3": ["MP3 V0"],
        "1 3": ["16bit 48.0 kHz", "MP3 V0"],
    }.items():
        # A pick by number is confirmed by a plain yes or no, as on master.
        result, terminal, described = ask(monkeypatch, conversions(held), [key, True])
        assert (key, result) == (key, outcome)
        assert [call[0] for call in terminal.calls] == ["prompt", "confirm"]
        assert terminal.calls[1][1] == click.style("Confirm selection?", fg="magenta")
        assert described.labels == [None]


def test_formats_to_convert_with_yyy_is_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload, "conversions", ["16bit", "V0"])
    result, terminal, _described = ask(monkeypatch, conversions(), [], yyy=True)
    assert result == ["16bit 48.0 kHz", "MP3 V0"]
    assert terminal.calls == []


# #36 (and #1) trackers.choose_tracker: next tracker


def test_tracker_choice_asks_as_on_master(monkeypatch: pytest.MonkeyPatch) -> None:
    result, terminal, _described = ask(monkeypatch, partial(trackers.choose_tracker, ["RED", "OPS"]), ["OPS"])
    assert result == "OPS"
    assert terminal.calls == [prompt_call("Your choices are RED , OPS or [n]one.", "RED")]


def test_every_key_of_tracker_choice_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    site = partial(trackers.choose_tracker, ["OPS", "DIC"])
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    assert keys(described.options[0]) == [("OPS", "OPS"), ("DIC", "DIC"), ("n", "None")]
    for key, outcome in {"OPS": "OPS", "DIC": "DIC", "n": None}.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1


# #7 dupe_checker._prompt_for_group_id and _prompt_for_recent_upload_results: upload to an existing group


def group_text(offer_deletion: bool, offer_skip: bool, found: bool = True) -> str:
    return (
        "\nWould you like to upload to an existing group?\n"
        f"Paste a URL{', pick from groups found ' if found else ''}"
        f"or [N]ew group / {'[s]kip this tracker / ' if offer_skip else ''}[a]bort "
        f"{'/ [d]elete music folder ' if offer_deletion else ''}"
    )


def recent_text(offer_deletion: bool, offer_skip: bool) -> str:
    return (
        "\nThese are similar recent uploads from the site log, not exact group matches.\n"
        "Pick one only if it is actually the same group.\n"
        "Pick from recent uploads found, paste a URL"
        f" or [N]ew group / {'[s]kip this tracker / ' if offer_skip else ''}[a]bort "
        f"{'/ [d]elete music folder ' if offer_deletion else ''}"
    )


@pytest.mark.parametrize("offer_deletion", [True, False])
@pytest.mark.parametrize("offer_skip", [True, False])
@pytest.mark.parametrize("default", ["", "3"])
def test_search_group_question_asks_as_on_master(
    monkeypatch: pytest.MonkeyPatch, offer_deletion: bool, offer_skip: bool, default: str
) -> None:
    result, terminal, _described = ask(monkeypatch, search_group(None, offer_deletion, default, offer_skip), ["n"])
    assert result is None
    assert terminal.calls == [prompt_call(group_text(offer_deletion, offer_skip), default)]


@pytest.mark.parametrize("offer_deletion", [True, False])
@pytest.mark.parametrize("offer_skip", [True, False])
def test_recent_uploads_question_asks_as_on_master(
    monkeypatch: pytest.MonkeyPatch, offer_deletion: bool, offer_skip: bool
) -> None:
    result, terminal, _described = ask(monkeypatch, recent_group(None, offer_deletion, offer_skip), ["n"])
    assert result is None
    assert terminal.calls == [prompt_call(recent_text(offer_deletion, offer_skip), "")]


@pytest.mark.parametrize("offer_deletion", [True, False])
@pytest.mark.parametrize("offer_skip", [True, False])
def test_every_key_of_the_search_group_question_is_accepted(
    monkeypatch: pytest.MonkeyPatch, offer_deletion: bool, offer_skip: bool
) -> None:
    site = search_group(None, offer_deletion, "", offer_skip)
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    options = described.options[0]
    # Row 2 is not printed (a KeyError): listed by number and link only, so a typed 2 is never read as that row.
    assert keys(options) == [
        ("1", "Tom & Jerry - Cat & Mouse"),
        ("2", "Result 2"),
        ("3", "B - Two"),
        ("n", "New group"),
        *([("s", "Skip this tracker")] if offer_skip else []),
        ("a", "Abort"),
        *([("d", "Delete music folder")] if offer_deletion else []),
    ]
    assert options is not None
    assert {option.group for option in options.items[:3]} == {"Search results on RED"}
    assert options.items[1].card == interaction.Card(link="https://red.test/torrents.php?id=22")
    assert options.items[0].card == interaction.Card(
        (("Year", "2020"), ("Release type", "Album"), ("Tags", "pop, rock & roll")),
        link="https://red.test/torrents.php?id=21",
    )
    assert [(o.key, o.action, o.danger, o.empty) for o in options.items[3:]] == [
        ("n", True, False, True),
        *([("s", True, False, False)] if offer_skip else []),
        ("a", True, False, False),
        *([("d", True, True, False)] if offer_deletion else []),
    ]
    assert options.free_text == interaction.FreeText("Group URL or ID", ids=True)
    expected = {"1": 21, "2": 22, "3": 23, "n": None, "a": click.Abort}
    expected |= {"s": SkipTracker} if offer_skip else {}
    expected |= {"d": AbortAndDeleteFolder} if offer_deletion else {}
    for key, outcome in expected.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1
    # What the web sends as a text: a group URL, or an ID above every numbered key.
    for text, outcome in {"https://red.test/torrents.php?id=42": 42, "4": 4}.items():
        result, terminal, _described = ask(monkeypatch, site, [text])
        assert (text, result) == (text, outcome)
    if not offer_skip:
        # Without the offer, an s is not read as a skip, as on master.
        result, terminal, _described = ask(monkeypatch, site, ["s", "n"])
        assert result is None
        assert len(terminal.calls) == 2


@pytest.mark.parametrize("offer_deletion", [True, False])
@pytest.mark.parametrize("offer_skip", [True, False])
def test_every_key_of_the_recent_uploads_question_is_accepted(
    monkeypatch: pytest.MonkeyPatch, offer_deletion: bool, offer_skip: bool
) -> None:
    site = recent_group(None, offer_deletion, offer_skip)
    _result, _terminal, described = ask(monkeypatch, site, ["n"])
    options = described.options[0]
    assert keys(options) == [
        ("1", "Artist - Album"),
        ("2", "Other - Thing & More"),
        ("n", "New group"),
        *([("s", "Skip this tracker")] if offer_skip else []),
        ("a", "Abort"),
        *([("d", "Delete music folder")] if offer_deletion else []),
    ]
    assert options is not None
    assert {option.group for option in options.items[:2]} == {"Recent uploads on RED"}
    assert options.items[0].card == interaction.Card(
        link="https://red.test/torrents.php?torrentid=31", note="Not an exact group match"
    )
    assert options.items[2].empty and options.free_text == interaction.FreeText("Group URL or ID", ids=True)
    expected = {"1": 1031, "2": 1032, "n": None, "a": click.Abort, "3": 3, "https://red.test/torrents.php?id=42": 42}
    expected |= {"s": SkipTracker} if offer_skip else {}
    expected |= {"d": AbortAndDeleteFolder} if offer_deletion else {}
    for key, outcome in expected.items():
        result, terminal, _described = ask(monkeypatch, site, [key])
        assert (key, result) == (key, outcome)
        assert len(terminal.calls) == 1


def test_recent_uploads_past_the_fifth_are_listed_by_number_and_link_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI prints five but reads a number up to the list's length as that row: the web must not send 6 as an ID.
    rows = [(n, "A", f"T{n}") for n in range(1, 8)]
    _result, _terminal, described = ask(monkeypatch, recent_group(rows), ["n"])
    options = described.options[0]
    assert [key for key, _label in keys(options)][:8] == ["1", "2", "3", "4", "5", "6", "7", "n"]
    assert options is not None
    assert options.items[5].card == interaction.Card(link="https://red.test/torrents.php?torrentid=6")
    assert options.items[5].label == "Recent upload 6"


def test_picking_a_search_row_without_a_group_id_asks_again(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    rows = [{"artist": "Broken"}, *SEARCH_RESULTS[:1]]
    result, terminal, _described = ask(monkeypatch, search_group(rows), ["1", "2"])
    assert result == 21
    assert len(terminal.calls) == 2
    out = capsys.readouterr().out
    assert "Result 1 has no group ID: pick another, or paste a group URL." in out


def test_confirming_a_group_does_not_fail_on_an_earlier_row_without_a_group_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A row without a groupId before the match used to raise KeyError while the picked row was looked up.
    results = [{"artist": "Broken"}, GROUP]
    printed: list[dict | None] = []

    async def fake_print_torrents(_site: Any, _group_id: int, rset: dict | None = None, *_args: Any) -> dict:
        printed.append(rset)
        return rset or GROUP

    monkeypatch.setattr(dupe_checker, "print_torrents", fake_print_torrents)
    for group_id in (7, 99):  # a picked row, then a typed group ID that is in no row
        printed.clear()
        site = partial(dupe_checker._confirm_group_id, SITE, group_id, results)
        result, terminal, _described = ask(monkeypatch, site, ["y"])
        assert result is True
        assert len(terminal.calls) == 1
        assert printed == [GROUP if group_id == 7 else None]


def test_the_group_questions_with_yyy_ask_as_on_master(monkeypatch: pytest.MonkeyPatch) -> None:
    # -yyy is not read by these prompts: the same call is made, with the same default, as without it.
    result, terminal, _described = ask(monkeypatch, search_group(None, True, "3"), [""], yyy=True)
    assert result is None
    assert terminal.calls == [prompt_call(group_text(True, False), "3")]
    result, terminal, _described = ask(monkeypatch, recent_group(), [""], yyy=True)
    assert result is None
    assert terminal.calls == [prompt_call(recent_text(True, False), "")]
