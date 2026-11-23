"""The Deezer parser's credits, as Deezer states them (#869 step 3): a name Deezer gives as one artist stays one
artist, its remixers are read, and the credits salmon guessed are marked."""

import pytest
from helpers.deezer import deezer_song, deezer_tracks

from salmon.tagger.sources.deezer import Scraper


def _artists(contributors, default_artists=(), title="Song"):
    return Scraper().parse_artists(contributors, list(default_artists), title)[0]


def _guesses(contributors, default_artists=(), title="Song"):
    return Scraper().parse_artists(contributors, list(default_artists), title)[1]


def test_a_contributor_whose_name_holds_a_comma_and_an_ampersand_is_one_artist() -> None:
    assert _artists({"main_artist": ["Earth, Wind & Fire"]}) == [("Earth, Wind & Fire", "main")]


def test_a_featured_contributor_whose_name_holds_a_comma_is_one_artist() -> None:
    assert _artists({"main_artist": ["Kanye West"], "featuring": ["Tyler, The Creator"]}) == [
        ("Kanye West", "main"),
        ("Tyler, The Creator", "guest"),
    ]


def test_a_track_artist_whose_name_holds_an_ampersand_is_one_artist() -> None:
    assert _artists({}, [{"ART_NAME": "Simon & Garfunkel"}]) == [("Simon & Garfunkel", "main")]


def test_a_title_s_featured_credit_names_each_guest_after_what_deezer_states() -> None:
    """The merge keeps a name where it was first claimed, so a guess doesn't lead the track."""
    assert _artists({"main_artist": ["Bob"]}, title="Song (feat. A & B)") == [
        ("Bob", "main"),
        ("A", "guest"),
        ("B", "guest"),
    ]


def test_a_guest_named_in_the_title_and_as_a_contributor_is_one_guest() -> None:
    contributors = {"main_artist": ["Kanye West"], "featuring": ["Tyler, The Creator"]}

    assert _artists(contributors, title="Song (feat. Tyler, The Creator)") == [
        ("Kanye West", "main"),
        ("Tyler, The Creator", "guest"),
    ]


def test_a_main_contributor_named_again_in_the_title_is_only_main() -> None:
    assert _artists({"main_artist": ["Tyler, The Creator"]}, title="Song (feat. Tyler, The Creator)") == [
        ("Tyler, The Creator", "main")
    ]


def test_a_title_naming_a_contributor_and_another_guest_keeps_the_other_guest() -> None:
    contributors = {"main_artist": ["Bob"], "featuring": ["Earth, Wind & Fire"]}

    assert _artists(contributors, title="Song (feat. Earth, Wind & Fire & Carol)") == [
        ("Bob", "main"),
        ("Earth, Wind & Fire", "guest"),
        ("Carol", "guest"),
    ]


def test_as_before_an_empty_contributor_is_no_artist() -> None:
    assert _artists({"main_artist": ["Bob", ""], "featuring": [""]}) == [("Bob", "main")]


def test_as_before_an_empty_track_artist_is_no_artist() -> None:
    assert _artists({}, [{"ART_NAME": "Bob"}, {"ART_NAME": ""}]) == [("Bob", "main")]


def test_as_before_a_contributor_s_stray_spaces_are_trimmed() -> None:
    assert _artists({"main_artist": [" Bob ", "  "], "featuring": ["Carol "]}) == [("Bob", "main"), ("Carol", "guest")]


def test_as_before_a_track_artist_s_stray_spaces_are_trimmed() -> None:
    assert _artists({}, [{"ART_NAME": " Bob "}, {"ART_NAME": " "}]) == [("Bob", "main")]


def test_a_title_guest_sharing_a_word_with_a_contributor_s_name_stays_a_guest() -> None:
    assert _artists({"main_artist": ["Alice & Bob"]}, title="Song (feat. Bob & Carol)") == [
        ("Alice & Bob", "main"),
        ("Bob", "guest"),
        ("Carol", "guest"),
    ]


@pytest.mark.parametrize(
    ("contributors", "expected"),
    [
        ({"main_artist": ["Lady Gaga"], "remixer": ["Guéna LG"]}, [("Lady Gaga", "main"), ("Guéna LG", "remixer")]),
        ({"main_artist": ["Bob"], "remixer": [" Carol ", "Carol", " "]}, [("Bob", "main"), ("Carol", "remixer")]),
        (
            {"main_artist": ["Dua Lipa", "The Blessed Madonna"], "remixer": ["The Blessed Madonna"]},
            [("Dua Lipa", "main"), ("The Blessed Madonna", "main"), ("The Blessed Madonna", "remixer")],
        ),
    ],
    ids=["stated", "once and trimmed", "a main who also remixes keeps both credits, for the merge to decide"],
)
def test_a_remixer_deezer_states_is_credited(contributors: dict, expected: list[tuple[str, str]]) -> None:
    assert _artists(contributors) == expected


def test_a_remixer_is_credited_so_its_tag_is_written_and_nothing_stated_is_marked_a_guess() -> None:
    track = deezer_tracks(
        deezer_song(1, title="Alejandro", contributors={"main_artist": ["Lady Gaga"], "remixer": ["Guéna LG"]})
    )["1"]["1"]

    assert track["credited_remixers"] == ["Guéna LG"]
    assert track["assumed_artists"] == []


def test_a_guest_only_the_title_names_is_marked_a_guess() -> None:
    track = deezer_tracks(deezer_song(1, title="Song (feat. Carol)", contributors={"main_artist": ["Bob"]}))["1"]["1"]

    assert track["artists"] == [("Bob", "main"), ("Carol", "guest")]
    assert track["assumed_artists"] == [("Carol", "guest")]


@pytest.mark.parametrize(
    ("contributors", "default_artists", "title", "expected"),
    [
        ({"main_artist": ["Bob"], "featuring": ["Carol"]}, [], "Song (feat. Carol)", []),
        # The merge reads the two spellings as one artist; the title's stays a guess, so Deezer's word wins.
        (
            {"main_artist": ["Bob"], "featuring": ["Ms. Lauryn Hill"]},
            [],
            "Song (feat. Lauryn Hill)",
            [("Lauryn Hill", "guest")],
        ),
        ({}, [{"ART_NAME": "Alice"}, {"ART_NAME": "Bob"}], "Song", [("Alice", "main"), ("Bob", "main")]),
        # The merge then keeps Deezer's stated main.
        ({"main_artist": ["Carol"]}, [], "Song (feat. Carol)", [("Carol", "guest")]),
        ({"main_artist": ["Alice"]}, [], "Song (feat. Bob & Bob)", [("Bob", "guest")]),
    ],
    ids=[
        "a guest the title names and deezer states is stated",
        "a title guest spelled apart from the stated guest",
        "the track's artists given as mains when it has no contributors",
        "a main the title also names as guest",
        "a guest the title names twice is one guess",
    ],
)
def test_what_salmon_only_guessed_is_marked(
    contributors: dict, default_artists: list[dict], title: str, expected: list[tuple[str, str]]
) -> None:
    assert _guesses(contributors, default_artists, title) == expected


def test_a_title_guest_deezer_states_as_main_comes_after_the_mains() -> None:
    assert _artists({"main_artist": ["Alice", "Bob"]}, title="Song (feat. Bob)") == [
        ("Alice", "main"),
        ("Bob", "main"),
        ("Bob", "guest"),
    ]


def test_a_guest_the_title_names_twice_is_credited_once() -> None:
    assert _artists({"main_artist": ["Alice"]}, title="Song (feat. Bob & Bob)") == [("Alice", "main"), ("Bob", "guest")]
