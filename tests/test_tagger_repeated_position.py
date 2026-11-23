"""A store answer that puts two tracks at one position is refused, naming the position (#968, part 2).

Each parser kept the second track and lost the first, saying nothing. Beatport numbers its tracks by its own count,
so it has no row: its answer can't repeat a position.
"""

import textwrap
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from bs4 import BeautifulSoup

from salmon.errors import ScrapeError
from salmon.tagger.sources import apple_music, bandcamp, deezer, discogs, musicbrainz, qobuz, tidal


def _apple(*positions: tuple[int, int]) -> tuple[Any, Any]:
    tracks = [
        {"type": "songs", "attributes": {"name": f"Song {i}", "artistName": "A", "discNumber": d, "trackNumber": n}}
        for i, (d, n) in enumerate(positions)
    ]
    return apple_music.Scraper(), {"tracks": tracks}


def _bandcamp(*numbers: str) -> tuple[Any, Any]:
    rows = "".join(
        f'<tr class="track_row_view"><td class="track-number-col"><div class="track_number">{n}</div></td>'
        f'<td class="title-col"><span class="track-title">Song {i}</span></td></tr>'
        for i, n in enumerate(numbers)
    )
    page = f"""
        <div id="name-section"><h2 class="trackTitle">Album</h2><h3>by <span><a>A</a></span></h3></div>
        <table id="track_table">{rows}</table>
    """
    return bandcamp.Scraper(), BeautifulSoup(textwrap.dedent(page), "lxml")


def _deezer(*positions: tuple[int, int]) -> tuple[Any, Any]:
    tracks = [
        {
            "DISK_NUMBER": d,
            "TRACK_NUMBER": n,
            "SNG_CONTRIBUTORS": {"main_artist": ["A"]},
            "ARTISTS": [{"ART_NAME": "A"}],
            "SNG_TITLE": f"Song {i}",
            "ISRC": None,
            "EXPLICIT_LYRICS": False,
            "SNG_ID": i,
        }
        for i, (d, n) in enumerate(positions)
    ]
    return deezer.Scraper(), {"tracklist": tracks, "genres": {"data": []}}


def _discogs(*positions: str) -> tuple[Any, Any]:
    tracks = [{"type_": "track", "position": p, "title": f"Song {i}", "duration": ""} for i, p in enumerate(positions)]
    return discogs.Scraper(), {"tracklist": tracks, "artists": [{"name": "A", "anv": "", "join": ""}]}


def _musicbrainz(*positions: tuple[str, str]) -> tuple[Any, Any]:
    discs: dict[str, list] = {}
    for i, (d, n) in enumerate(positions):
        recording = {"title": f"Song {i}", "artist-credit": [{"artist": {"name": "A"}}]}
        discs.setdefault(d, []).append({"number": n, "recording": recording})
    return musicbrainz.Scraper(), {"media": [{"position": d, "tracks": t} for d, t in discs.items()]}


def _qobuz(*positions: dict[str, int]) -> tuple[Any, Any]:
    items = [{"title": f"Song {i}", "performer": {"name": "A"}, **p} for i, p in enumerate(positions)]
    return qobuz.Scraper(), {"artist": {"name": "A"}, "tracks": {"items": items}}


def _tidal(*positions: tuple[int, int]) -> tuple[Any, Any]:
    tracks = [
        {
            "volumeNumber": d,
            "trackNumber": n,
            "artists": [{"name": "A", "main": True}],
            "title": f"Song {i}",
            "version": None,
            "isrc": None,
            "explicit": False,
            "audioQuality": None,
            "id": i,
        }
        for i, (d, n) in enumerate(positions)
    ]
    return tidal.Scraper(), {"tracklist": tracks}


Answer = Callable[[], tuple[Any, Any]]

# Each row: a store's answer with two tracks at one position, and what the refusal names.
REPEATS = [
    pytest.param(lambda: _apple((1, 1), (1, 1)), "Disc 1 has two tracks numbered 1.", id="apple-music"),
    pytest.param(lambda: _bandcamp("1.", "1."), "Disc 1 has two tracks numbered 1.", id="bandcamp"),
    pytest.param(lambda: _deezer((1, 1), (1, 1)), "Disc 1 has two tracks numbered 1.", id="deezer"),
    pytest.param(lambda: _discogs("1", "2", "2"), "Disc 1 has two tracks numbered 2.", id="discogs"),
    pytest.param(lambda: _discogs("1", "2", "02"), "Disc 1 has two tracks numbered 2.", id="discogs-padded"),
    pytest.param(lambda: _discogs("A1", "A2", "A2"), "Disc 1 has two tracks numbered A2.", id="discogs-side"),
    pytest.param(lambda: _discogs("", ""), "Disc 1 has two tracks with no number.", id="discogs-blank"),
    pytest.param(lambda: _discogs("12.1", "12.2", "12.2"), "Disc 1 has two tracks numbered 12.2.", id="discogs-dotted"),
    pytest.param(lambda: _discogs("-", "-"), "Disc 1 has two tracks with no number.", id="discogs-dash"),
    pytest.param(lambda: _musicbrainz(("1", "1"), ("1", "1")), "Disc 1 has two tracks numbered 1.", id="musicbrainz"),
    pytest.param(lambda: _musicbrainz(("1", ""), ("1", "")), "Disc 1 has two tracks with no number.", id="mb-blank"),
    pytest.param(
        lambda: _qobuz({"track_number": 1}, {"track_number": 1}), "Disc 1 has two tracks numbered 1.", id="qobuz"
    ),
    pytest.param(lambda: _tidal((1, 1), (1, 1)), "Disc 1 has two tracks numbered 1.", id="tidal"),
]


@pytest.mark.parametrize(("answer", "refusal"), REPEATS)
def test_a_store_answer_with_two_tracks_at_one_position_is_refused(answer: Answer, refusal: str) -> None:
    scraper, soup = answer()

    with pytest.raises(ScrapeError) as refused:
        anyio.run(scraper.parse_tracks, soup)

    assert str(refused.value) == refusal


# The same number on two discs is two positions.
TWO_DISCS = [
    pytest.param(lambda: _apple((1, 1), (2, 1)), id="apple-music"),
    pytest.param(lambda: _deezer((1, 1), (2, 1)), id="deezer"),
    pytest.param(lambda: _discogs("1-1", "2-1"), id="discogs"),
    pytest.param(lambda: _musicbrainz(("1", "1"), ("2", "1")), id="musicbrainz"),
    pytest.param(
        lambda: _qobuz({"media_number": 1, "track_number": 1}, {"media_number": 2, "track_number": 1}), id="qobuz"
    ),
    pytest.param(lambda: _tidal((1, 1), (2, 1)), id="tidal"),
]


def test_the_same_entry_given_twice_is_refused_too() -> None:
    scraper, soup = _discogs("1", "2")
    soup["tracklist"].append(soup["tracklist"][1])

    with pytest.raises(ScrapeError, match="Disc 1 has two tracks numbered 2."):
        anyio.run(scraper.parse_tracks, soup)


@pytest.mark.parametrize("answer", TWO_DISCS)
def test_as_before_the_same_number_on_two_discs_keeps_both_tracks(answer: Answer) -> None:
    scraper, soup = answer()

    tracks = anyio.run(scraper.parse_tracks, soup)

    assert sorted((disc, number) for disc, numbered in tracks.items() for number in map(str, numbered)) == [
        ("1", "1"),
        ("2", "1"),
    ]
