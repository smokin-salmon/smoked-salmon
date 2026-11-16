"""The Deezer parser's credits: a name Deezer gives as one artist stays one artist."""

from salmon.tagger.sources.deezer import Scraper


def _artists(contributors, default_artists=(), title="Song"):
    return Scraper().parse_artists(contributors, list(default_artists), title)


def test_a_contributor_whose_name_holds_a_comma_and_an_ampersand_is_one_artist() -> None:
    assert _artists({"main_artist": ["Earth, Wind & Fire"]}) == [("Earth, Wind & Fire", "main")]


def test_a_featured_contributor_whose_name_holds_a_comma_is_one_artist() -> None:
    assert _artists({"main_artist": ["Kanye West"], "featuring": ["Tyler, The Creator"]}) == [
        ("Kanye West", "main"),
        ("Tyler, The Creator", "guest"),
    ]


def test_an_album_artist_whose_name_holds_an_ampersand_is_one_artist() -> None:
    assert _artists({}, [{"ART_NAME": "Simon & Garfunkel"}]) == [("Simon & Garfunkel", "main")]


def test_as_before_a_title_s_featured_credit_still_names_each_guest() -> None:
    assert _artists({"main_artist": ["Bob"]}, title="Song (feat. A & B)") == [
        ("A", "guest"),
        ("B", "guest"),
        ("Bob", "main"),
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
        ("Carol", "guest"),
        ("Bob", "main"),
        ("Earth, Wind & Fire", "guest"),
    ]


def test_as_before_an_empty_contributor_is_no_artist() -> None:
    assert _artists({"main_artist": ["Bob", ""], "featuring": [""]}) == [("Bob", "main")]


def test_as_before_an_empty_album_artist_is_no_artist() -> None:
    assert _artists({}, [{"ART_NAME": "Bob"}, {"ART_NAME": ""}]) == [("Bob", "main")]


def test_as_before_a_guest_without_a_separator_named_in_the_title_and_as_a_contributor_comes_first() -> None:
    assert _artists({"main_artist": ["Bob"], "featuring": ["Carol"]}, title="Song (feat. Carol)") == [
        ("Carol", "guest"),
        ("Bob", "main"),
    ]
