"""MusicBrainz search on recorded answers (#1065): what salmon makes of a real search today.

The answers are in tests/fixtures/stores/musicbrainz/search/, XML and JSON fetched together. Moving the client to
the JSON must leave these results as they are.
"""

from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from helpers.musicbrainz import serving_musicbrainz

from salmon import cfg
from salmon.search import musicbrainz as musicbrainz_search

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
