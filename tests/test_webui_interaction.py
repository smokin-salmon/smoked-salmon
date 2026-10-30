"""Typed questions in salmon web: the description, what the default preselects, the checks and the line (ADR 0006).

The call site keeps its text and its parser: the web sends the browser a description of the answers it accepts, and
turns what the browser picked into the line a terminal user would have typed. These tests drive WebInteraction with a
scripted browser, and the server's answer route through test_webui_jobs's harness (local server, no tracker).
"""

import json
from collections.abc import Callable
from typing import Any

import anyio
import asyncclick as click
import msgspec
import pytest
from aiohttp.test_utils import TestClient
from test_webui_jobs import (  # pyright: ignore[reportMissingImports]
    AUTH,
    SECRETS,
    Params,
    Run,
    _finished,
    _question,
    _start,
    _with_app,
    kind,  # noqa: F401 (a fixture)
    no_learned_secrets,  # noqa: F401 (an autouse fixture)
    planted,  # noqa: F401 (a fixture)
)

from salmon import interaction
from salmon.interaction import Card, FreeText, Mark, Option, Options
from salmon.webui.interaction import Picked, WebInteraction
from salmon.webui.jobs import JobManager


class Browser:
    """Answers each question from a script, and keeps what it was asked."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.questions: list[dict[str, Any]] = []

    async def ask(self, question: dict[str, Any]) -> Any:
        self.questions.append(question)
        return self.answers.pop(0)

    def show_spectrals(self, folder: str, files: list[str]) -> None:
        pass


def _prompt(options: Options | None, answers: list[Any], **kwargs: Any) -> tuple[Any, Browser]:
    """Ask a prompt with `options` in the web; returns what the prompt returned and the browser."""
    browser = Browser(answers)

    async def ask() -> Any:
        return await WebInteraction(browser).prompt("Which?", options=options, **kwargs)

    return anyio.run(ask), browser


# Questions like the ones the call sites ask: one single, one several with a mark and URLs (as #12 will be).
SINGLE = Options(
    (
        Option("y", "Upload to this group"),
        Option("n", "New group"),
        Option("a", "Abort", action=True),
        Option("d", "Delete music folder", action=True, danger=True),
    )
)
RESULTS = Options(
    (
        Option("1", "Artist - One", group="Search results", card=Card((("Year", "2020"),), link="https://t.test/1")),
        Option("2", "Artist - Two", group="Search results", card=Card(note="Not an exact match")),
        Option("n", "New group", action=True, empty=True),
    ),
    free_text=FreeText("Group URL or ID", ids=True),
)
STORES = Options(
    (
        Option("3", "Artist - Album", group="Bandcamp"),
        Option("7", "Artist - Album", group="Qobuz"),
        Option("9", "Artist - Album", group="Tidal"),
        Option("m", "Manual", action=True),
    ),
    several=True,
    free_text=FreeText("Store URL"),
    mark=Mark("*", "The store the files came from (WEB)"),
)
SPECTRALS = Options(
    (
        Option("1", "01 One"),
        Option("2", "02 Two"),
        Option("3", "03 Three"),
        Option("*", "All", alone=True),
        Option("0", "None", alone=True),
        Option("+", "Random third", alone=True),
    ),
    several=True,
)


# --- The description refuses what it cannot mean ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "why"),
    [
        (lambda: Options((Option("", "Nothing"),)), "empty"),
        (lambda: Options((Option("a b", "Spaced"),)), "space"),
        (lambda: Options((Option("a\t", "Tabbed"),)), "space"),
        (lambda: Options((Option("a", "One"), Option("a", "Two"))), "twice"),
        (lambda: Options((Option("n", "One"), Option("N", "Two"))), "twice"),
        (lambda: Options((Option("a", "One", empty=True), Option("b", "Two", empty=True))), "empty reply"),
        (lambda: Options((Option("*", "All", alone=True),)), "alone"),
        (lambda: Options((Option("1", "One"),), mark=Mark("*", "Store")), "mark goes only"),
        (lambda: Options((Option("d", "Delete", danger=True),)), "danger"),
        (lambda: Options((Option("1", "One"),), several=True, mark=Mark("", "Store")), "prefix"),
        (lambda: Options((Option("1", "One"),), several=True, mark=Mark("* ", "Store")), "prefix"),
        (lambda: Options((Option("*1", "One"),), several=True, mark=Mark("*", "Store")), "prefix"),
    ],
)
def test_a_description_that_cannot_be_read_back_is_refused(build: Callable[[], Options], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        build()


def test_the_descriptions_the_call_sites_build_are_accepted() -> None:
    assert [option.key for option in SINGLE.items] == ["y", "n", "a", "d"]
    assert RESULTS.free_text == FreeText("Group URL or ID", ids=True)
    assert STORES.mark == Mark("*", "The store the files came from (WEB)")
    assert [option.alone for option in SPECTRALS.items] == [False, False, False, True, True, True]


# --- What the browser is sent ----------------------------------------------------------------------------------------


def test_a_question_without_options_is_sent_exactly_as_before() -> None:
    result, browser = _prompt(None, ["typed"], default="x")
    assert result == "typed"
    assert browser.questions == [{"kind": "prompt", "text": "Which?", "default": "x", "choices": None, "error": None}]


def test_a_confirm_without_labels_is_sent_exactly_as_before_and_with_labels_names_its_buttons() -> None:
    browser = Browser([True, False])

    async def ask() -> list[bool]:
        web = WebInteraction(browser)
        return [
            await web.confirm(click.style("Upload?", fg="magenta"), default=True),
            await web.confirm("Upload?", default=True, labels=("Upload", "Re-run the metadata step")),
        ]

    assert anyio.run(ask) == [True, False]
    assert browser.questions == [
        {"kind": "confirm", "text": "Upload?", "default": True, "error": None},
        {
            "kind": "confirm",
            "text": "Upload?",
            "default": True,
            "error": None,
            "labels": ["Upload", "Re-run the metadata step"],
        },
    ]


def test_the_description_is_sent_as_plain_json_with_every_field() -> None:
    _result, browser = _prompt(RESULTS, [Picked(picks=["1"])], default="")
    question = browser.questions[0]
    assert {key: question[key] for key in ("kind", "text", "default", "choices", "error")} == {
        "kind": "prompt",
        "text": "Which?",
        "default": "",
        "choices": None,
        "error": None,
    }
    # Dicts and lists only, as the egress filter walks them, and the same once through JSON.
    assert json.loads(json.dumps(question["options"])) == question["options"]
    assert question["options"] == {
        "items": [
            {
                "key": "1",
                "label": "Artist - One",
                "group": "Search results",
                "card": {"fields": [["Year", "2020"]], "link": "https://t.test/1", "note": None},
                "action": False,
                "alone": False,
                "empty": False,
                "danger": False,
            },
            {
                "key": "2",
                "label": "Artist - Two",
                "group": "Search results",
                "card": {"fields": [], "link": None, "note": "Not an exact match"},
                "action": False,
                "alone": False,
                "empty": False,
                "danger": False,
            },
            {
                "key": "n",
                "label": "New group",
                "group": None,
                "card": None,
                "action": True,
                "alone": False,
                "empty": True,
                "danger": False,
            },
        ],
        "several": False,
        "free_text": {"placeholder": "Group URL or ID", "ids": True},
        "mark": None,
    }


@pytest.mark.parametrize(
    ("link", "sent"),
    [
        ("https://red.test/torrents.php?id=1", "https://red.test/torrents.php?id=1"),
        ("http://store.test/album", "http://store.test/album"),
        ("HTTPS://store.test/album", "HTTPS://store.test/album"),
        ("javascript:alert(1)", None),
        (" javascript:alert(1)", None),
        ("JavaScript:alert(1)", None),
        ("data:text/html,<script>alert(1)</script>", None),
        ("//store.test/album", None),
        ("https://", None),
        ("https://store.test/a b", None),
        ("https://store.test/a\nb", None),
        (" https://store.test/album", None),
        ("torrents.php?id=1", None),
    ],
)
def test_only_an_http_card_link_is_sent(link: str, sent: str | None) -> None:
    options = Options((Option("1", "One", card=Card(link=link, note="kept")),))
    _result, browser = _prompt(options, [Picked(picks=["1"])])
    assert browser.questions[0]["options"]["items"][0]["card"] == {"fields": [], "link": sent, "note": "kept"}


# --- What the default preselects -------------------------------------------------------------------------------------


def _preselected(options: Options, default: Any) -> dict[str, Any]:
    _result, browser = _prompt(options, [Picked(picks=[options.items[0].key])], default=default)
    return browser.questions[0]["preselected"]


def _sel(picks: list[str] = [], texts: list[str] = [], mark: str | None = None) -> dict[str, Any]:  # noqa: B006
    return {"picks": picks, "texts": texts, "mark": mark}


@pytest.mark.parametrize(
    ("options", "default", "preselected"),
    [
        # No default, nothing preselected.
        (SINGLE, None, _sel()),
        # A key, ignoring case, as the declared key.
        (SINGLE, "Y", _sel(["y"])),
        (SINGLE, "a", _sel(["a"])),
        # An empty default is the empty option, if there is one.
        (RESULTS, "", _sel(["n"])),
        (SINGLE, "", _sel()),
        (RESULTS, "   ", _sel(["n"])),
        # Anything else is a text when the question takes one, else dropped.
        (RESULTS, "https://red.test/torrents.php?id=5", _sel(texts=["https://red.test/torrents.php?id=5"])),
        (SINGLE, "maybe", _sel()),
        # A single question keeps the first pick or text.
        (RESULTS, "2 1", _sel(["2"])),
        (RESULTS, "https://x.test/a 1", _sel(texts=["https://x.test/a"])),
        # Several: every pick, in the default's order, then the texts; the marked one noted without its prefix.
        (STORES, "*3 7 https://x.bandcamp.com/album/y", _sel(["3", "7"], ["https://x.bandcamp.com/album/y"], "3")),
        (
            STORES,
            "7 *https://x.bandcamp.com/album/y",
            _sel(["7"], ["https://x.bandcamp.com/album/y"], "https://x.bandcamp.com/album/y"),
        ),
        (STORES, "3 3", _sel(["3"])),
        (STORES, "*", _sel()),
        # An alone option is kept on its own.
        (SPECTRALS, "+", _sel(["+"])),
        (SPECTRALS, "*", _sel(["*"])),
        (SPECTRALS, "0", _sel(["0"])),
        (SPECTRALS, "1 3", _sel(["1", "3"])),
        (SPECTRALS, "1 * 3", _sel(["*"])),
        (SPECTRALS, "4 1", _sel(["1"])),
        # A default that is not a string is read as its text.
        (Options((Option("1", "One"), Option("2", "Two"))), 2, _sel(["2"])),
    ],
)
def test_the_default_preselects_what_the_terminal_would_read_in_it(
    options: Options, default: Any, preselected: dict[str, Any]
) -> None:
    assert _preselected(options, default) == preselected


def test_the_preselection_follows_the_default_even_when_it_is_not_shown() -> None:
    _result, browser = _prompt(SINGLE, [Picked(picks=["y"])], default="Y", show_default=False)
    assert browser.questions[0]["default"] is None
    assert browser.questions[0]["preselected"] == _sel(["y"])


# --- The answer: what is refused, and the line ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "answer", "error"),
    [
        # 1. Not a Picked: a text, even a key, or a yes or no.
        (SINGLE, "y", "Pick from the options."),
        (SINGLE, "delete-me", "Pick from the options."),
        (SINGLE, True, "Pick from the options."),
        # 2. A pick that is not a key, or twice.
        (SINGLE, Picked(picks=["x"]), "x is not one of the options."),
        (SINGLE, Picked(picks=["Y"]), "Y is not one of the options."),
        (STORES, Picked(picks=["3", "3"]), "Artist - Album is picked twice."),
        # 3. An action or an alone option with anything else.
        (SINGLE, Picked(picks=["y", "d"]), "Delete music folder goes on its own."),
        (STORES, Picked(picks=["m"], texts=["https://x.test/a"]), "Manual goes on its own."),
        (STORES, Picked(picks=["m"], mark="m"), "Manual goes on its own."),
        (SPECTRALS, Picked(picks=["1", "*"]), "All goes on its own."),
        (RESULTS, Picked(picks=["n"], texts=["123"]), "New group goes on its own."),
        # 4. Single: exactly one pick or text. Several: at least one.
        (SINGLE, Picked(), "Pick one option."),
        (SINGLE, Picked(picks=["y", "n"]), "Pick one option."),
        (RESULTS, Picked(picks=["1"], texts=["https://x.test/a"]), "Pick one option, or type one answer."),
        (RESULTS, Picked(texts=["https://x.test/a", "https://x.test/b"]), "Pick one option, or type one answer."),
        (SPECTRALS, Picked(), "Pick at least one option."),
        # 5. A text the question takes no text for, or one that could be read as a key.
        (SINGLE, Picked(texts=["https://x.test/a"]), "This question takes no typed answer: pick from the options."),
        (STORES, Picked(texts=["3"]), "3 is not a URL: paste one that starts with http:// or https://."),
        (STORES, Picked(texts=["https://x.test/a b"]), "is not a URL"),
        (STORES, Picked(texts=["javascript:alert(1)"]), "is not a URL"),
        (RESULTS, Picked(texts=["2"]), "2 is result 2 above: pick it there, or paste the URL."),
        (RESULTS, Picked(texts=["02"]), "2 is result 2 above: pick it there, or paste the URL."),
        (RESULTS, Picked(texts=["0"]), "An ID is a number above 2: paste the URL, or pick from the options."),
        (RESULTS, Picked(texts=["n"]), "n is neither a URL nor an ID."),
        (RESULTS, Picked(texts=["-5"]), "-5 is neither a URL nor an ID."),
        (RESULTS, Picked(texts=["٣"]), "neither a URL nor an ID"),
        (RESULTS, Picked(texts=[" 5"]), "neither a URL nor an ID"),
        # 6. A mark the question has no Mark for, or one that is not exactly one answer sent.
        (SPECTRALS, Picked(picks=["1"], mark="1"), "This question takes no mark."),
        (STORES, Picked(picks=["3"], mark="7"), "Mark one of the answers sent as The store the files came from (WEB)."),
        (
            STORES,
            Picked(texts=["https://x.test/a", "https://x.test/a"], mark="https://x.test/a"),
            "Mark one of the answers sent",
        ),
    ],
)
def test_a_wrong_answer_is_asked_again_with_its_error_also_in_the_log(
    options: Options, answer: Any, error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    good = Picked(picks=[next(option.key for option in options.items if not option.action and not option.alone)])
    _result, browser = _prompt(options, [answer, good], default="")
    assert len(browser.questions) == 2
    assert browser.questions[0]["error"] is None
    assert error in browser.questions[1]["error"]
    assert browser.questions[1]["error"].startswith("Error: ")
    # The same question again, as the terminal asks again.
    assert browser.questions[1]["text"] == browser.questions[0]["text"]
    assert browser.questions[1]["options"] == browser.questions[0]["options"]
    assert browser.questions[1]["error"] in capsys.readouterr().out


@pytest.mark.parametrize(
    ("options", "answer", "line"),
    [
        # Single: the key, or the text.
        (SINGLE, Picked(picks=["y"]), "y"),
        (SINGLE, Picked(picks=["d"]), "d"),
        (RESULTS, Picked(picks=["2"]), "2"),
        (RESULTS, Picked(texts=["https://red.test/torrents.php?id=5"]), "https://red.test/torrents.php?id=5"),
        (RESULTS, Picked(texts=["3"]), "3"),
        (RESULTS, Picked(texts=["12345"]), "12345"),
        # Several: the picks in the options' order, then the texts in the order sent, the marked one prefixed.
        (SPECTRALS, Picked(picks=["3", "1"]), "1 3"),
        (SPECTRALS, Picked(picks=["+"]), "+"),
        (SPECTRALS, Picked(picks=["0"]), "0"),
        (
            STORES,
            Picked(picks=["7", "3"], texts=["https://x.bandcamp.com/album/y"], mark="3"),
            "*3 7 https://x.bandcamp.com/album/y",
        ),
        (
            STORES,
            Picked(picks=["9"], texts=["https://b.test/2", "https://a.test/1"], mark="https://a.test/1"),
            "9 https://b.test/2 *https://a.test/1",
        ),
        (STORES, Picked(texts=["https://a.test/1"]), "https://a.test/1"),
        (STORES, Picked(picks=["m"]), "m"),
    ],
)
def test_the_answer_becomes_the_line_a_terminal_user_would_type(options: Options, answer: Picked, line: str) -> None:
    result, browser = _prompt(options, [answer])
    assert result == line
    assert len(browser.questions) == 1


def test_the_line_goes_through_the_conversion_as_a_typed_answer_does() -> None:
    options = Options((Option("1", "One"), Option("2", "Two")))
    result, _browser = _prompt(options, [Picked(picks=["2"])], type=int)
    assert result == 2

    seen: list[str] = []

    def value_proc(line: str) -> str:
        seen.append(line)
        return line.upper()

    result, _browser = _prompt(SPECTRALS, [Picked(picks=["1", "3"])], value_proc=value_proc)
    assert (result, seen) == ("1 3", ["1 3"])


@pytest.mark.parametrize("empty", ["", None])
def test_an_empty_answer_takes_the_call_sites_default_as_enter_does(empty: str | None) -> None:
    # The page never sends one; an API client that does gets the default, never a text of its own.
    result, browser = _prompt(SINGLE, [empty], default="Y")
    assert (result, len(browser.questions)) == ("Y", 1)
    result, browser = _prompt(SPECTRALS, [empty], default="1 3")
    assert (result, len(browser.questions)) == ("1 3", 1)


@pytest.mark.parametrize("empty", ["", None])
def test_an_empty_answer_without_a_default_is_asked_again(
    empty: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    result, browser = _prompt(SINGLE, [empty, Picked(picks=["n"])])
    assert result == "n"
    assert browser.questions[1]["error"] == "Error: Pick from the options."
    assert "Error: Pick from the options." in capsys.readouterr().out


def test_a_plain_question_refuses_picks_and_asks_again() -> None:
    result, browser = _prompt(None, [Picked(picks=["y"]), "typed"])
    assert result == "typed"
    assert browser.questions[1]["error"] == "Error: Type the answer."


# --- Through the server --------------------------------------------------------------------------------------------


def test_the_server_takes_picks_as_an_answer_and_refuses_unknown_fields(
    kind: Callable[[str, Run], None],  # noqa: F811
) -> None:
    async def run(_params: Params) -> str:
        return await interaction.prompt("Upload to this group?", default="Y", options=SINGLE)

    kind("typed", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "typed")
        question = await _question(manager, job_id)
        assert question["preselected"] == _sel(["y"])
        url = f"/api/jobs/{job_id}/answer"
        refused = await client.post(
            url, json={"question_id": question["id"], "value": {"picks": ["n"], "extra": 1}}, headers=AUTH
        )
        assert refused.status == 400
        wrong = await client.post(url, json={"question_id": question["id"], "value": {"picks": "n"}}, headers=AUTH)
        assert wrong.status == 400
        response = await client.post(
            url,
            json={"question_id": question["id"], "value": {"picks": ["n"], "texts": [], "mark": None}},
            headers=AUTH,
        )
        assert response.status == 200, await response.text()
        assert (await _finished(manager, job_id)).result == "n"

    _with_app(test)


def test_no_secret_in_a_description_leaves_the_server(
    kind: Callable[[str, Run], None],  # noqa: F811
    planted: None,  # noqa: F811
) -> None:
    secret = SECRETS["api_key"]
    options = Options(
        (
            Option("1", f"label {secret}", group=f"group {secret}", card=Card(((secret, secret),), note=secret)),
            Option("n", "None"),
        ),
        free_text=FreeText(f"placeholder {secret}"),
    )

    async def run(_params: Params) -> str:
        return await interaction.prompt("Which?", default="n", options=options)

    kind("typed", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "typed")
        question = await _question(manager, job_id)
        sent = await (await client.get(f"/api/jobs/{job_id}", headers=AUTH)).text()
        assert secret not in sent
        assert secret not in json.dumps(question)
        assert "[REDACTED]" in json.dumps(question["options"])
        response = await client.post(
            f"/api/jobs/{job_id}/answer", json={"question_id": question["id"], "value": {"picks": ["1"]}}, headers=AUTH
        )
        assert response.status == 200
        assert (await _finished(manager, job_id)).result == "1"

    _with_app(test)


def test_picked_is_what_the_answer_route_decodes() -> None:
    from salmon.webui.server import AnswerRequest

    decoded = msgspec.json.decode(b'{"question_id": "q", "value": {"picks": ["1"], "mark": "1"}}', type=AnswerRequest)
    assert decoded.value == Picked(picks=["1"], texts=[], mark="1")
    for value in (b'"y"', b"true", b"null"):
        assert msgspec.json.decode(b'{"question_id": "q", "value": ' + value + b"}", type=AnswerRequest).value in (
            "y",
            True,
            None,
        )
