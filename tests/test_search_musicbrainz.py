"""MusicBrainz search on recorded answers (#1065): what salmon makes of a real search today.

The answers are in tests/fixtures/stores/musicbrainz/search/, XML and JSON fetched together. Moving the client to
the JSON must leave these results as they are.
"""

import random
import re
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from helpers.musicbrainz import serving_musicbrainz, slug

from salmon import cfg
from salmon.search import deezer as deezer_search
from salmon.search import musicbrainz as musicbrainz_search
from salmon.search.hints import SearchHints
from salmon.search.musicbrainz import fielded_query

SEARCH = Path(__file__).parent / "fixtures" / "stores" / "musicbrainz" / "search"


def _search(slug: str, query: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[dict[str, Any], str]]:
    assert (SEARCH / f"{slug}.json").is_file()
    with serving_musicbrainz(monkeypatch) as server:
        store, releases = anyio.run(musicbrainz_search.Searcher().search_releases, query, 10)
    assert store == "MusicBrainz"
    [(path, sent, _headers)] = server.requests
    assert path == "/ws/2/release"
    assert sent == {"query": query, "limit": "10", "fmt": "json"}
    return {
        mbid: (
            {f: getattr(ident, f) for f in ident.__struct_fields__},
            click.unstyle(line),
        )
        for mbid, (ident, line) in releases.items()
    }


KIND_OF_BLUE: dict[str, tuple[dict[str, Any], str]] = {
    "9e003d73-c007-4fb2-a5f2-4f0b0f9a2649": (
        {
            "artist": "Peter Sprague",
            "album": "Blue Kind of Miles",
            "year": None,
            "track_count": 6,
            "source": "Digital Media",
        },
        "Peter Sprague - Blue Kind of Miles {Tracks: 6} {Digital Media}",
    ),
    "48819492-44a9-4a90-839c-5f179ee561b5": (
        {
            "artist": "Twinkle Twinkle Little Rock Star",
            "album": "Lullaby Versions of Kind of Blue & Miles Davis",
            "year": None,
            "track_count": 8,
            "source": "Digital Media",
        },
        "Twinkle Twinkle Little Rock Star - Lullaby Versions of Kind of Blue & Miles Davis {Tracks: 8} {Digital Media}",
    ),
    "b485a424-9fbd-4603-8ae3-d00096b294ac": (
        {"artist": "Miles Davis", "album": "Miles Davis", "year": None, "track_count": 24, "source": "CD"},
        "Miles Davis - Miles Davis {Tracks: 24} {CD} Madacy Entertainment DDP2 52456",
    ),
    "d1e17f64-fd7d-423e-93fe-dea5c16e18c6": (
        {"artist": "Miles Davis", "album": "Miles Davis", "year": None, "track_count": 15, "source": "CD"},
        "Miles Davis - Miles Davis {Tracks: 15} {CD} United Audio Entertainment UAE 30472",
    ),
    "34016314-dc7c-40f2-a47c-fd96e7e78d9f": (
        {"artist": "Miles Davis", "album": "Miles Davis", "year": None, "track_count": 24, "source": "CD"},
        "Miles Davis - Miles Davis {Tracks: 24} {CD} Disky DSP 904765",
    ),
    "f091bee5-001f-4334-a6c3-0ee676a6f397": (
        {"artist": "Miles Davis", "album": "Miles Davis", "year": None, "track_count": 16, "source": "CD"},
        "Miles Davis - Miles Davis {Tracks: 16} {CD} Haba’na 200 570",
    ),
    "151d7a0d-4422-48e9-98e9-68f981687a92": (
        {"artist": "Miles Davis", "album": "Miles Davis", "year": None, "track_count": 10, "source": ""},
        "Miles Davis - Miles Davis {Tracks: 10}",
    ),
    "233e8db8-447c-460c-88ae-ee349f5b81fc": (
        {"artist": "Miles Davis", "album": "Miles Davis", "year": None, "track_count": 19, "source": "CD"},
        "Miles Davis - Miles Davis {Tracks: 19} {CD} Musicbank APWCD1191",
    ),
    "8706d1e6-65f1-43b9-bd58-815789d18db1": (
        {"artist": "Miles Davis", "album": "Miles Davis", "year": None, "track_count": 10, "source": "CD"},
        "Miles Davis - Miles Davis {Tracks: 10} {CD} Jazz Line JL 003",
    ),
    "e8b506b2-0a31-4141-a0b0-4bb8e24e2e0f": (
        {"artist": "Miles Davis", "album": "Miles Davis", "year": None, "track_count": 9, "source": "CD"},
        "Miles Davis - Miles Davis {Tracks: 9} {CD} wea music 0630-15645 2",
    ),
}
QUEEN: dict[str, tuple[dict[str, Any], str]] = {
    "75c4bd73-aa6c-4a67-b26e-796313861fbf": (
        {
            "artist": "Carol Douglas",
            "album": "Disco Queen: Greatest Hits",
            "year": None,
            "track_count": 10,
            "source": "",
        },
        "Carol Douglas - Disco Queen: Greatest Hits {Tracks: 10}",
    ),
    "92655853-3fcc-3491-b166-0c932420f594": (
        {"artist": "Queen", "album": "Greatest Hits II", "year": None, "track_count": 17, "source": "CD"},
        "Queen - Greatest Hits II {Tracks: 17} {CD} Parlophone CDPMTV 2",
    ),
    "d2aaa62b-0a22-4731-a45e-0554a4fffbb0": (
        {"artist": "Queen", "album": "Greatest Hits I & II", "year": None, "track_count": 34, "source": "CD"},
        "Queen - Greatest Hits I & II {Tracks: 34} {CD} Hollywood Records HR-62042-2",
    ),
    "a8e5772e-757f-4ba8-8452-56b785d13d0a": (
        {
            "artist": "Queen",
            "album": "Queen: Greatest Video Hits",
            "year": None,
            "track_count": 33,
            "source": "DVD-Video",
        },
        "Queen - Queen: Greatest Video Hits {Tracks: 33} {DVD-Video} Eagle Rock Entertainment",
    ),
    "eace2379-7b55-44e6-b258-240d6e796562": (
        {
            "artist": "The Royal Philharmonic Orchestra",
            "album": "Symphonic Queen: The Greatest Hits",
            "year": None,
            "track_count": 19,
            "source": "CD",
        },
        "The Royal Philharmonic Orchestra - Symphonic Queen: The Greatest Hits {Tracks: 19} {CD} Panorama 479 6268",
    ),
    "82609ad0-8286-4c22-91dd-e435289a22d5": (
        {
            "artist": "The Royal Philharmonic Orchestra, Matthew Freeman",
            "album": "Symphonic Queen: The Greatest Hits",
            "year": None,
            "track_count": 19,
            "source": "CD",
        },
        "The Royal Philharmonic Orchestra, Matthew Freeman - Symphonic "
        "Queen: The Greatest Hits {Tracks: 19} {CD} Panorama 479 6268",
    ),
    "52e2fcb5-860d-4d30-a86b-d7fdae255429": (
        {"artist": "Greatest Hits", "album": "Danse Pop", "year": None, "track_count": 4, "source": '7" Vinyl'},
        'Greatest Hits - Danse Pop {Tracks: 4} {7" Vinyl} Olde English Spelling Bee OESB-702',
    ),
    "85472d01-d29f-4206-af30-6287e1426253": (
        {"artist": "Greatest Hits", "album": "Spicy", "year": None, "track_count": 1, "source": "Digital Media"},
        "Greatest Hits - Spicy {Tracks: 1} {Digital Media} [no label]",
    ),
    "2b0854fe-8f66-4c4d-a775-abfe6b76cdcc": (
        {"artist": "Greatest Hits", "album": "Banana Moon", "year": None, "track_count": 1, "source": "Digital Media"},
        "Greatest Hits - Banana Moon {Tracks: 1} {Digital Media} [no label]",
    ),
    "961df75e-5b4c-491c-97d1-42210af61f76": (
        {"artist": "Greatest Hits", "album": "Palm Springs", "year": None, "track_count": 1, "source": "Digital Media"},
        "Greatest Hits - Palm Springs {Tracks: 1} {Digital Media} [no label]",
    ),
}
# The one result on Parlophone.
PARLOPHONE_IN_QUEEN: list[str] = ["92655853-3fcc-3491-b166-0c932420f594"]


@pytest.mark.parametrize(
    ("slug", "query", "expected"),
    [
        ("miles-davis-kind-of-blue", "Miles Davis Kind of Blue", KIND_OF_BLUE),
        ("queen-greatest-hits", "Queen Greatest Hits", QUEEN),
    ],
)
def test_search_parses_the_recorded_answer(
    slug: str, query: str, expected: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    results = _search(slug, query, monkeypatch)
    assert results == expected


def test_a_result_on_an_excluded_label_is_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload.search, "excluded_labels", ["parlophone"])
    results = _search("queen-greatest-hits", "Queen Greatest Hits", monkeypatch)
    assert set(results) == set(QUEEN) - set(PARLOPHONE_IN_QUEEN)


# The fielded query MusicBrainz is sent when salmon searches from the files (#1178).

_SPECIAL = '+-&|!(){}[]^"~*?:\\/'


def test_the_fielded_query_has_every_field_in_order() -> None:
    assert fielded_query(SearchHints("Miles Davis", "Kind of Blue", 5, "1959")) == (
        "artist:(miles davis) release:(kind of blue) tracks:(5) date:(1959)"
    )


def test_a_missing_field_is_left_out() -> None:
    assert fielded_query(SearchHints("Miles Davis", "Kind of Blue", 5, None)) == (
        "artist:(miles davis) release:(kind of blue) tracks:(5)"
    )
    assert fielded_query(SearchHints(None, None, None, None)) == ""
    assert fielded_query(SearchHints("", "", None, "")) == ""
    assert fielded_query(SearchHints("  ", " ", None, " ")) == ""
    assert fielded_query(SearchHints(" Miles Davis ", None, None, None)) == "artist:(miles davis)"


def test_special_characters_are_escaped() -> None:
    assert fielded_query(SearchHints("AC/DC", 'Live: "1991" (Deluxe) [Disc 1]', None, None)) == (
        r"artist:(ac\/dc) release:(live\: \"1991\" \(deluxe\) \[disc 1\])"
    )


@pytest.mark.parametrize("title", ["DO NOT DISTURB", "HIT ME HARD AND SOFT", "THIS OR THAT"])
def test_an_upper_case_operator_word_is_sent_as_a_word(title: str) -> None:
    assert not re.search(r"\b(AND|OR|NOT)\b", fielded_query(SearchHints("X", title, None, None)))


def _generated_values(count: int):
    rng = random.Random(1178)
    alphabet = ["a", "Z", " ", "é", "1", *_SPECIAL]
    for _ in range(count):
        yield "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))


def _unescaped_parentheses_balance(query: str) -> bool:
    depth, chars = 0, iter(query)
    for char in chars:
        if char == "\\":
            next(chars, None)
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def test_every_special_character_round_trips_and_parentheses_balance() -> None:
    for value in _generated_values(3000):
        query = fielded_query(SearchHints(None, value, None, None))
        if not value.strip():
            assert query == "", (value, query)
            continue
        assert query.startswith("release:(") and query.endswith(")"), (value, query)
        assert re.sub(r"\\(.)", r"\1", query[len("release:(") : -1]) == value.strip().lower(), (value, query)
        assert _unescaped_parentheses_balance(query), (value, query)


def test_without_hints_the_searchstrs_are_the_queries() -> None:
    assert musicbrainz_search.Searcher().queries(["a b", "c b"], None) == ["a b", "c b"]


def test_with_hints_one_fielded_query_replaces_every_searchstr() -> None:
    hints = SearchHints("A", "B", 3, "2000")
    assert musicbrainz_search.Searcher().queries(["a b", "c b"], hints) == [fielded_query(hints)]


def test_with_hints_that_say_nothing_the_searchstrs_are_the_queries() -> None:
    assert musicbrainz_search.Searcher().queries(["a b"], SearchHints(None, None, None, None)) == ["a b"]


@pytest.mark.parametrize(
    "hints",
    [SearchHints(None, None, 12, None), SearchHints(None, None, 12, "1999")],
    ids=["track count only", "track count and year"],
)
def test_without_an_artist_or_album_the_searchstrs_are_the_queries(hints: SearchHints) -> None:
    assert musicbrainz_search.Searcher().queries(["a b"], hints) == ["a b"]


@pytest.mark.parametrize(
    "hints",
    [SearchHints("A", None, 12, None), SearchHints(None, "B", 12, None)],
    ids=["artist", "album"],
)
def test_an_artist_or_album_is_enough_for_the_fielded_query(hints: SearchHints) -> None:
    assert musicbrainz_search.Searcher().queries(["a b"], hints) == [fielded_query(hints)]


def test_another_store_ignores_hints() -> None:
    assert deezer_search.Searcher().queries(["a b"], SearchHints("A", "B", 1, "2000")) == ["a b"]


@pytest.mark.parametrize(
    ("hints", "first", "title"),
    [
        (SearchHints("Miles Davis", "Kind of Blue", 5, "1959"), "ca6b19c7", "Kind of Blue"),
        (SearchHints("Miles Davis", "Kind of Blue", 5, None), "e32a3f0b", "Kind of Blue"),
        (SearchHints("AC/DC", "Back in Black", 10, "1980"), "f7c680af", "Back in Black"),
        (
            SearchHints("Various Artists", "Pulp Fiction (Music From the Motion Picture)", 16, "1994"),
            "161cc82c",
            "Pulp Fiction: Music From the Motion Picture",
        ),
    ],
)
def test_a_fielded_search_parses_the_recorded_answer(
    hints: SearchHints, first: str, title: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = fielded_query(hints)
    results = _search(slug(query), query, monkeypatch)
    assert len(results) == 10
    assert next(iter(results)).startswith(first)
    idents = [ident for ident, _line in results.values()]
    assert all(ident["album"] == title and ident["track_count"] == hints.track_count for ident in idents[:3])
