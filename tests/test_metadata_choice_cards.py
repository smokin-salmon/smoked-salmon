"""The metadata results question (#12) in the terminal and in salmon web, and upload.search.preferred_sources and
skipped_sources (#759, ADR 0006).

The terminal asks exactly as on master: the text and default asyncclick gets are master's, copied here, and every key
the web's description lists is accepted when typed. salmon web gets cards grouped by store, several selectable, a URL
field and the mark, and the picks reach the call site's own parser as the line a terminal user would type. The stores
and their scrapers are fakes: nothing is fetched.
"""

import re
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import pytest
from test_config_compat import _base, _load  # pyright: ignore[reportMissingImports]
from test_interaction_typed_sites import ask, keys, prompt_call  # pyright: ignore[reportMissingImports]
from test_webui_interaction import Browser  # pyright: ignore[reportMissingImports]
from test_webui_search import FOUND, _fake_stores  # pyright: ignore[reportMissingImports]

import salmon.search
from salmon import cfg, interaction
from salmon.config.validations import SEARCH_STORES
from salmon.search import SEARCHSOURCES, metas, run_metasearch
from salmon.search.base import IdentData
from salmon.tagger import metadata
from salmon.webui import server
from salmon.webui.interaction import Picked, WebInteraction, _line

# master's two questions, as asyncclick is given them.
QUESTION = (
    "\nWhich metadata results would you like to use? Type result numbers and/or paste URLs, "
    'separated by spaces; all of them are combined. Put "*" before the one the files came from '
    "(the WEB store). Or [m]anual / [a]bort."
)
NO_RESULTS = (
    "\nNo metadata results were found. Paste URLs separated by spaces (all of them are combined), "
    'with "*" before the one the files came from (the WEB store). Or [m]anual / [a]bort.'
)


def _found(artist: str, album: str, year: int | None, tracks: int | None) -> tuple[IdentData, str]:
    return IdentData(artist, album, year, tracks, "WEB"), f"{artist} - {album} ({year}) [{tracks} tracks]"


BC_1 = ("artist.bandcamp.com", "album", "the-album")
BC_2 = ("artist.bandcamp.com", "album", "another")
BC_3 = ("other.bandcamp.com", "album", "the-album")
# What run_metasearch returns, in SEARCHSOURCES order: seven results over three stores, one store without results,
# one inactive, one failed (Apple Music, left out).
RESULTS: dict[str, Any] = {
    "Bandcamp": {
        BC_1: _found("Artist", "Album", 2020, 10),
        BC_2: _found("Artist", "Another", 2019, 8),
        BC_3: _found("Someone", "Album", None, None),
    },
    "MusicBrainz": {},
    "Discogs": None,
    "Beatport": {},
    "Qobuz": {
        "q1": _found("Artist", "Album", 2020, 10),
        "q2": _found("Artist", "Album (Deluxe)", 2020, 14),
        "q3": _found("Artist", "Album (Live)", 2021, 12),
    },
    "Tidal": {},
    "Deezer": {"d1": _found("Artist", "Album", 2020, 10)},
}

QOBUZ_URL = "https://www.qobuz.com/album/-/q1"
STORE_URL = "https://x.bandcamp.com/album/y"

# What _print_search_results prints for RESULTS on master, without the colours.
MASTER_LISTING = """
Results for Bandcamp:
> 01 Artist - Album (2020) [10 tracks] | https://artist.bandcamp.com/album/the-album
> 02 Artist - Another (2019) [8 tracks] | https://artist.bandcamp.com/album/another
> 03 Someone - Album (None) [None tracks] | https://other.bandcamp.com/album/the-album

Results for Qobuz:
> 04 Artist - Album (2020) [10 tracks] | https://www.qobuz.com/album/-/q1
> 05 Artist - Album (Deluxe) (2020) [14 tracks] | https://www.qobuz.com/album/-/q2
> 06 Artist - Album (Live) (2021) [12 tracks] | https://www.qobuz.com/album/-/q3

Results for Deezer:
> 07 Artist - Album (2020) [10 tracks] | https://www.deezer.com/album/d1

No results found from MusicBrainz.
No results found from Apple Music.
No results found from Beatport.
No results found from Tidal.
Discogs is inactive. Add its credentials to config.toml to enable it.

Failed to scrape Apple Music.
"""


@pytest.fixture(autouse=True)
def _interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload.search, "limit", 3)


class Scrapes:
    """Fake metadata sources for the stores in RESULTS: each scrape is recorded, nothing is fetched."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, Any]] = []
        monkeypatch.setattr(
            metadata,
            "METASOURCES",
            {
                "Bandcamp": self._source("Bandcamp", r"https?://[^/]+\.bandcamp\.com/"),
                "Qobuz": self._source("Qobuz", r"https?://www\.qobuz\.com/"),
                "Deezer": self._source("Deezer", r"https?://www\.deezer\.com/"),
            },
        )
        # The scrapes as they come, without combining them.
        monkeypatch.setattr(
            metadata,
            "_combine_scraped",
            lambda sources, metadatas, _rls_data, source_url: {"scraped": metadatas, "source_url": source_url},
        )

        async def manual(_rls_data: dict[str, Any]) -> dict[str, Any]:
            return {"manual": True}

        monkeypatch.setattr(metadata, "_get_manual_metadata", manual)

    def _source(self, name: str, pattern: str) -> Any:
        calls = self.calls

        class Scraper:
            regex = re.compile(pattern)

            async def scrape_release_from_id(self, rls_id: Any) -> Any:
                calls.append((name, rls_id))
                return (name, rls_id)

            async def scrape_release(self, url: str) -> Any:
                calls.append((name, url))
                return (name, url)

        return SimpleNamespace(Scraper=Scraper)


@pytest.fixture
def scrapes(monkeypatch: pytest.MonkeyPatch) -> Scrapes:
    return Scrapes(monkeypatch)


def _choices(capsys: pytest.CaptureFixture[str] | None = None) -> dict[int, tuple[str, str]]:
    choices = metadata._print_search_results(RESULTS)
    if capsys is not None:
        capsys.readouterr()
    return choices


def _select(choices: dict[int, tuple[str, str]], default: str | None) -> Any:
    return partial(metadata._select_choice, choices, {"urls": []}, default)


def _rls_data(source: str = "WEB") -> dict[str, Any]:
    return {"artists": [("Artist", "main")], "title": "Album", "year": 2020, "source": source, "urls": []}


def _url(source: str, rls_id: Any) -> str:
    return SEARCHSOURCES[source].Searcher.format_url(rls_id)


def _options(described: Any) -> interaction.Options:
    """The description the first question came with."""
    options = described.options[0]
    assert options is not None
    return options


# --- The terminal: as on master ---------------------------------------------------------------------------------


@pytest.mark.parametrize("default", [None, "4", f"*{QOBUZ_URL} 1"])
def test_the_results_question_gives_asyncclick_masters_text_and_default(monkeypatch, scrapes, default) -> None:
    result, terminal, _ = ask(monkeypatch, _select(_choices(), default), ["m"])
    assert result == ({"manual": True}, None)
    assert terminal.calls == [prompt_call(QUESTION, default, click.STRING)]


@pytest.mark.parametrize("default", [None, f"*{QOBUZ_URL}"])
def test_the_no_results_question_gives_asyncclick_masters_text_and_default(monkeypatch, scrapes, default) -> None:
    result, terminal, _ = ask(monkeypatch, _select({}, default), ["a"])
    assert result is click.Abort
    assert terminal.calls == [prompt_call(NO_RESULTS, default, click.STRING)]


@pytest.mark.parametrize("found", [True, False], ids=["results", "no results"])
def test_every_key_the_web_lists_is_accepted_in_the_terminal(monkeypatch, scrapes, found) -> None:
    choices = _choices() if found else {}
    _, _, described = ask(monkeypatch, _select(choices, None), ["m"])
    listed = [option.key for option in _options(described).items]
    assert listed == ([str(n) for n in range(1, 8)] if found else []) + ["m", "a"]
    for key in listed:
        scrapes.calls.clear()
        result, terminal, _ = ask(monkeypatch, _select(choices, None), [key])
        assert len(terminal.calls) == 1, key
        if key == "m":
            assert result == ({"manual": True}, None)
        elif key == "a":
            assert result is click.Abort
        else:
            source, rls_id = choices[int(key)]
            assert scrapes.calls == [(source, rls_id)]
            assert result == ({"scraped": [(source, rls_id)], "source_url": None}, None)


@pytest.mark.parametrize("found", [True, False], ids=["results", "no results"])
def test_a_marked_store_url_typed_is_taken_as_on_master(monkeypatch, scrapes, found) -> None:
    result, terminal, _ = ask(monkeypatch, _select(_choices() if found else {}, None), [f"*{STORE_URL}"])
    assert len(terminal.calls) == 1
    assert scrapes.calls == [("Bandcamp", STORE_URL)]
    assert result == ({"scraped": [("Bandcamp", STORE_URL)], "source_url": STORE_URL}, STORE_URL)


def test_under_yyy_the_question_is_still_asked_as_on_master(monkeypatch, scrapes) -> None:
    """master has no -yyy branch here: the metadata question is asked, with its default, either way."""
    default = f"*{QOBUZ_URL} 1"
    result, terminal, _ = ask(monkeypatch, _select(_choices(), default), [default], yyy=True)
    assert terminal.calls == [prompt_call(QUESTION, default, click.STRING)]
    assert result[1] == QOBUZ_URL


# --- The description -------------------------------------------------------------------------------------------


def test_the_results_are_cards_grouped_by_store_with_manual_abort_a_url_field_and_the_mark(
    monkeypatch, scrapes
) -> None:
    _, _, described = ask(monkeypatch, _select(_choices(), None), ["m"])
    options = _options(described)
    assert options.several
    assert keys(options) == [
        ("1", "Artist - Album"),
        ("2", "Artist - Another"),
        ("3", "Someone - Album"),
        ("4", "Artist - Album"),
        ("5", "Artist - Album (Deluxe)"),
        ("6", "Artist - Album (Live)"),
        ("7", "Artist - Album"),
        ("m", "Manual"),
        ("a", "Abort"),
    ]
    by_key = {option.key: option for option in options.items}
    assert [by_key[k].group for k in "1234567"] == ["Bandcamp"] * 3 + ["Qobuz"] * 3 + ["Deezer"]
    assert by_key["1"].card == interaction.Card(
        (("Year", "2020"), ("Tracks", "10")), link="https://artist.bandcamp.com/album/the-album"
    )
    # A year or track count the store does not give is left out.
    assert by_key["3"].card == interaction.Card(link="https://other.bandcamp.com/album/the-album")
    assert by_key["4"].card is not None and by_key["4"].card.link == QOBUZ_URL
    assert [(o.key, o.action, o.card) for o in options.items if o.action] == [("m", True, None), ("a", True, None)]
    assert options.free_text == interaction.FreeText("Store URL")
    assert options.mark == interaction.Mark("*", "The store the files came from (WEB)")


def test_the_no_results_question_has_manual_abort_the_url_field_and_the_mark(monkeypatch, scrapes) -> None:
    _, _, described = ask(monkeypatch, _select({}, None), ["a"])
    options = _options(described)
    assert keys(options) == [("m", "Manual"), ("a", "Abort")]
    assert options.several and options.free_text is not None and options.mark is not None


def test_a_result_the_search_did_not_describe_shows_its_number_and_link() -> None:
    options = metadata._choice_options({1: ("Qobuz", "q9")})
    assert keys(options)[0] == ("1", "Result 1")
    assert options.items[0].card == interaction.Card(link="https://www.qobuz.com/album/-/q9")


# --- salmon web ------------------------------------------------------------------------------------------------


def web(site: Any, answers: list[Any]) -> tuple[Any, Browser]:
    browser = Browser(answers)

    async def run() -> Any:
        with interaction.using(WebInteraction(browser)):
            try:
                return await site()
            except click.Abort as e:
                return type(e)

    return anyio.run(run), browser


@pytest.mark.parametrize(
    ("source", "preselected"),
    [
        ("WEB", {"picks": ["1"], "texts": [QOBUZ_URL], "mark": QOBUZ_URL}),
        ("CD", {"picks": ["1"], "texts": [QOBUZ_URL], "mark": None}),
    ],
)
def test_the_web_preselects_what_suggest_choice_gives(monkeypatch, scrapes, source, preselected) -> None:
    """With the files' Qobuz URL: starred for a WEB release only, and the matching result of another store."""
    choices = _choices()
    default = metadata.suggest_choice(choices, RESULTS, _rls_data(source), 10, QOBUZ_URL)
    assert default == (f"*{QOBUZ_URL} 1" if source == "WEB" else f"{QOBUZ_URL} 1")
    _, browser = web(_select(choices, default), [Picked(picks=["m"])])
    assert browser.questions[0]["preselected"] == preselected
    assert browser.questions[0]["default"] == default


def test_the_web_preselects_the_matching_result_alone_without_a_store_url(monkeypatch, scrapes) -> None:
    choices = _choices()
    # Bandcamp and Qobuz each have one match; the first listed is Bandcamp's.
    default = metadata.suggest_choice(choices, RESULTS, _rls_data(), 10, None)
    assert default == "1"
    _, browser = web(_select(choices, default), [Picked(picks=["m"])])
    assert browser.questions[0]["preselected"] == {"picks": ["1"], "texts": [], "mark": None}


def test_the_web_preselects_nothing_without_a_default(monkeypatch, scrapes) -> None:
    _, browser = web(_select(_choices(), None), [Picked(picks=["a"])])
    assert browser.questions[0]["preselected"] == {"picks": [], "texts": [], "mark": None}


def test_picks_urls_and_a_mark_reach_the_parser_as_the_line_typed(monkeypatch, scrapes) -> None:
    choices = _choices()
    answer = Picked(picks=["7", "3"], texts=[STORE_URL], mark="3")
    _, _, described = ask(monkeypatch, _select(choices, None), ["m"])
    assert _line(_options(described), answer) == (f"*3 7 {STORE_URL}", None)

    result, browser = web(_select(choices, None), [answer])
    assert len(browser.questions) == 1
    assert scrapes.calls == [("Bandcamp", BC_3), ("Deezer", "d1"), ("Bandcamp", STORE_URL)]
    marked = _url("Bandcamp", BC_3)
    assert result == ({"scraped": scrapes.calls, "source_url": marked}, marked)


def test_a_marked_url_in_the_no_results_question_reaches_the_parser(monkeypatch, scrapes) -> None:
    result, browser = web(_select({}, None), [Picked(texts=[STORE_URL], mark=STORE_URL)])
    assert len(browser.questions) == 1
    assert result == ({"scraped": [("Bandcamp", STORE_URL)], "source_url": STORE_URL}, STORE_URL)


def test_a_mark_on_something_not_picked_is_refused_and_asked_again(monkeypatch, scrapes) -> None:
    result, browser = web(_select(_choices(), None), [Picked(picks=["1"], mark="2"), Picked(picks=["1"], mark="1")])
    assert len(browser.questions) == 2
    assert (
        browser.questions[1]["error"] == "Error: Mark one of the answers sent as The store the files came from (WEB)."
    )
    assert scrapes.calls == [("Bandcamp", BC_1)]
    assert result[1] == _url("Bandcamp", BC_1)


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (Picked(picks=["m", "1"]), "Manual goes on its own."),
        (Picked(picks=["a"], texts=[STORE_URL]), "Abort goes on its own."),
        (Picked(texts=["3"]), "3 is not a URL: paste one that starts with http:// or https://."),
        ("3", "Pick from the options."),
    ],
)
def test_the_web_refuses_what_the_terminal_line_could_misread(monkeypatch, scrapes, answer, error) -> None:
    _, browser = web(_select(_choices(), None), [answer, Picked(picks=["m"])])
    assert browser.questions[1]["error"] == f"Error: {error}"
    assert scrapes.calls == []


def test_manual_and_abort_answer_at_once_in_the_web(monkeypatch, scrapes) -> None:
    assert web(_select(_choices(), None), [Picked(picks=["m"])])[0] == ({"manual": True}, None)
    assert web(_select(_choices(), None), [Picked(picks=["a"])])[0] is click.Abort


# --- preferred_sources -----------------------------------------------------------------------------------------


def test_without_preferred_sources_the_listing_is_masters(capsys) -> None:
    choices = metadata._print_search_results(RESULTS)
    assert click.unstyle(capsys.readouterr().out) == MASTER_LISTING
    assert list(choices.values()) == [
        ("Bandcamp", BC_1),
        ("Bandcamp", BC_2),
        ("Bandcamp", BC_3),
        ("Qobuz", "q1"),
        ("Qobuz", "q2"),
        ("Qobuz", "q3"),
        ("Deezer", "d1"),
    ]


def _listed(out: str) -> list[tuple[str, str, str]]:
    """Each heading's store and each result line's number and URL, in the order printed."""
    rows = []
    store = ""
    for line in click.unstyle(out).splitlines():
        if heading := re.fullmatch(r"Results for (.+):", line):
            store = heading[1]
        elif row := re.fullmatch(r"> (\d\d) .* \| (\S+)", line):
            rows.append((store, row[1], row[2]))
    return rows


def test_preferred_sources_are_listed_first_in_their_order_and_marked(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg.upload.search, "preferred_sources", ["Deezer", "Qobuz"])
    metadata._print_search_results(RESULTS)
    assert _listed(capsys.readouterr().out) == [
        ("Deezer (preferred)", "01", "https://www.deezer.com/album/d1"),
        ("Qobuz (preferred)", "02", "https://www.qobuz.com/album/-/q1"),
        ("Qobuz (preferred)", "03", "https://www.qobuz.com/album/-/q2"),
        ("Qobuz (preferred)", "04", "https://www.qobuz.com/album/-/q3"),
        ("Bandcamp", "05", "https://artist.bandcamp.com/album/the-album"),
        ("Bandcamp", "06", "https://artist.bandcamp.com/album/another"),
        ("Bandcamp", "07", "https://other.bandcamp.com/album/the-album"),
    ]


@pytest.mark.parametrize("preferred", [["Bandcamp"], ["Deezer", "Qobuz"], ["Qobuz", "Tidal"]])
def test_typing_a_printed_number_picks_the_result_printed_beside_it(monkeypatch, scrapes, capsys, preferred) -> None:
    monkeypatch.setattr(cfg.upload.search, "preferred_sources", preferred)
    choices = metadata._print_search_results(RESULTS)
    listed = _listed(capsys.readouterr().out)
    assert listed[0][0] == f"{preferred[0]} (preferred)"
    for _store, number, url in listed:
        scrapes.calls.clear()
        result, _, _ = ask(monkeypatch, _select(choices, None), [number])
        (source, rls_id), *_ = scrapes.calls
        assert _url(source, rls_id) == url
        assert result[0]["scraped"] == [(source, rls_id)]


def test_the_web_groups_a_preferred_store_under_its_marked_heading(monkeypatch, scrapes) -> None:
    monkeypatch.setattr(cfg.upload.search, "preferred_sources", ["Qobuz"])
    _, browser = web(_select(_choices(), None), [Picked(picks=["1"])])
    items = browser.questions[0]["options"]["items"]
    assert [(o["key"], o["group"]) for o in items[:4]] == [
        ("1", "Qobuz (preferred)"),
        ("2", "Qobuz (preferred)"),
        ("3", "Qobuz (preferred)"),
        ("4", "Bandcamp"),
    ]
    assert items[0]["card"]["link"] == QOBUZ_URL
    assert scrapes.calls == [("Qobuz", "q1")]


# --- skipped_sources -------------------------------------------------------------------------------------------


def test_a_skipped_store_is_never_searched(monkeypatch) -> None:
    searches = _fake_stores(monkeypatch)
    monkeypatch.setattr(cfg.upload.search, "skipped_sources", ["Discogs", "Tidal"])
    results = anyio.run(partial(run_metasearch, ["the album"], track_count=10))
    searched = {name for name, *_ in searches}
    assert searched == set(SEARCHSOURCES) - {"Discogs", "Tidal"}
    assert "Discogs" not in results and "Tidal" not in results


def test_without_skipped_sources_every_store_is_searched(monkeypatch) -> None:
    searches = _fake_stores(monkeypatch)
    anyio.run(partial(run_metasearch, ["the album"], track_count=10))
    assert {name for name, *_ in searches} == set(SEARCHSOURCES)


def test_every_store_skipped_searches_nothing(monkeypatch) -> None:
    searches = _fake_stores(monkeypatch)
    monkeypatch.setattr(cfg.upload.search, "skipped_sources", list(SEARCH_STORES))
    assert anyio.run(partial(run_metasearch, ["the album"])) == {}
    assert searches == []


def test_a_skipped_store_is_never_shown_in_the_listing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg.upload.search, "skipped_sources", ["Tidal", "Discogs", "Apple Music"])
    results = {name: found for name, found in RESULTS.items() if name not in ("Tidal", "Discogs")}
    metadata._print_search_results(results)
    out = click.unstyle(capsys.readouterr().out)
    assert "Tidal" not in out and "Discogs" not in out and "Apple Music" not in out
    assert "No results found from MusicBrainz." in out


def test_salmon_metas_skips_and_orders_the_stores(monkeypatch, capsys) -> None:
    searches = _fake_stores(monkeypatch)
    monkeypatch.setattr(cfg.upload.search, "skipped_sources", ["Discogs", "Apple Music"])
    monkeypatch.setattr(cfg.upload.search, "preferred_sources", ["Deezer"])
    assert metas.callback is not None
    anyio.run(metas.callback, ("the", "album"), 10, 3)
    out = click.unstyle(capsys.readouterr().out)
    assert {name for name, *_ in searches} == set(SEARCHSOURCES) - {"Discogs", "Apple Music"}
    assert "Searching Bandcamp, MusicBrainz, Beatport, Qobuz, Tidal, Deezer " in out
    assert "Discogs" not in out.split("(searchstrs")[1] and "Apple Music" not in out
    headings = re.findall(r"Results from (.+):", out)
    assert headings == ["Deezer (preferred)", "Bandcamp", "Beatport", "Tidal"]


def test_salmon_metas_without_the_settings_searches_and_lists_as_before(monkeypatch, capsys) -> None:
    _fake_stores(monkeypatch)
    assert metas.callback is not None
    anyio.run(metas.callback, ("the", "album"), 10, 3)
    out = click.unstyle(capsys.readouterr().out)
    assert f"Searching {', '.join(SEARCHSOURCES)} " in out
    assert re.findall(r"Results from (.+):", out) == ["Bandcamp", "Beatport", "Tidal", "Deezer"]
    assert "Failed to scrape Apple Music." in out


def test_the_web_search_skips_and_orders_the_stores(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.search, "skipped_sources", ["Discogs"])
    monkeypatch.setattr(cfg.upload.search, "preferred_sources", ["Tidal", "Bandcamp"])
    found = {name: answer for name, answer in FOUND.items() if not isinstance(answer, Exception)}
    results = {
        name: None
        if answer is None
        else {rls_id: (IdentData(a, b, y, t, "WEB"), f"{a} - {b}") for rls_id, (a, b, y, t) in answer.items()}
        for name, answer in found.items()
    }
    sources = server._search_answer(results)
    assert [(s["name"], s["status"], s["preferred"]) for s in sources] == [
        ("Tidal", "found", True),
        ("Bandcamp", "found", True),
        ("MusicBrainz", "none", False),
        ("Apple Music", "failed", False),
        ("Beatport", "found", False),
        ("Qobuz", "inactive", False),
        ("Deezer", "found", False),
    ]


def test_the_web_search_without_the_settings_lists_every_store_unmarked() -> None:
    sources = server._search_answer({})
    assert [(s["name"], s["preferred"]) for s in sources] == [(name, False) for name in SEARCHSOURCES]


# --- The config ------------------------------------------------------------------------------------------------


def test_the_stores_the_config_knows_are_the_stores_salmon_searches() -> None:
    assert tuple(salmon.search.SEARCHSOURCES) == SEARCH_STORES


def test_by_default_no_store_is_preferred_or_skipped(tmp_path: Path, capsys) -> None:
    loaded, warnings = _load(tmp_path, _base(tmp_path), capsys)
    assert loaded.upload.search.preferred_sources == []
    assert loaded.upload.search.skipped_sources == []
    assert warnings == []


def test_store_names_are_read_ignoring_case_each_once(tmp_path: Path, capsys) -> None:
    raw = _base(tmp_path)
    raw["upload"] = {
        "search": {"preferred_sources": ["qobuz", "Bandcamp", "QOBUZ"], "skipped_sources": ["apple music"]}
    }
    loaded, _ = _load(tmp_path, raw, capsys)
    assert loaded.upload.search.preferred_sources == ["Qobuz", "Bandcamp"]
    assert loaded.upload.search.skipped_sources == ["Apple Music"]


@pytest.mark.parametrize(
    ("search", "error"),
    [
        ({"preferred_sources": ["Spotify"]}, 'upload.search.preferred_sources names "Spotify", which is not a store'),
        ({"skipped_sources": ["Qobuz", "Juno"]}, 'upload.search.skipped_sources names "Juno", which is not a store'),
        (
            {"preferred_sources": ["Qobuz"], "skipped_sources": ["qobuz"]},
            "upload.search names Qobuz in both preferred_sources and skipped_sources",
        ),
    ],
)
def test_an_unknown_or_doubly_listed_store_is_refused_at_start(tmp_path: Path, capsys, search, error) -> None:
    raw = _base(tmp_path)
    raw["upload"] = {"search": search}
    with pytest.raises(ValueError, match=re.escape(error)):
        _load(tmp_path, raw, capsys)
