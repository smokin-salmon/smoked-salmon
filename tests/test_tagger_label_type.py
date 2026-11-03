"""Whether a label names no label, or the artist's own, is decided by whole words (#924)."""

import pytest

from salmon.tagger.sources import deezer, tidal
from salmon.tagger.sources.base import determine_label_type

AIR = [("Air", "main")]
SCRAPERS = [pytest.param(deezer.Scraper, id="Deezer"), pytest.param(tidal.Scraper, id="Tidal")]


@pytest.mark.parametrize(
    "label",
    [
        # Inside a word: "tech[no label]", "pia[no label]", "casi[no label]", "my[self released]".
        "Techno Label Records",
        "Piano Label",
        "Casino Label Group",
        "Myself Released",
        # The wording running on, as a plural.
        "No Labels",
    ],
)
def test_a_no_label_wording_inside_a_word_is_a_label(label: str) -> None:
    assert determine_label_type(label, AIR) == label


@pytest.mark.parametrize(
    ("label", "artist"),
    [
        ("Airplane Records", "Air"),
        ("AIRPLANE", "air"),
        ("Air2 Records", "Air"),
        # Running straight on from a name ending in a letter can't be told from "Airplane", so it stays a label.
        ("坂本龍一Records", "坂本"),
        ("坂本龍一Records", "坂本龍一"),
    ],
)
def test_a_label_that_only_starts_with_the_letters_of_an_artists_name_is_a_label(label: str, artist: str) -> None:
    assert determine_label_type(label, [(artist, "main")]) == label


@pytest.mark.parametrize(
    "label",
    [
        "Not On Label",
        "NOT ON LABEL",
        "No Label",
        "no label",
        "self-released",
        "Self Released",
        "selfreleased",
        # Anywhere in the label, as a whole phrase.
        "[no label]",
        "Moby Self-Released",
    ],
)
def test_a_no_label_wording_is_self_released_as_before(label: str) -> None:
    assert determine_label_type(label, AIR) == "Self-Released"


@pytest.mark.parametrize(
    ("label", "artist"),
    [
        ("Air", "Air"),
        ("air records", "Air"),
        ("Air-Records", "Air"),
        ("Air/Sounds", "Air"),
        ("AC/DC Records", "AC/DC"),
        ("Simon & Garfunkel Inc.", "Simon & Garfunkel"),
        ("坂本龍一 Records", "坂本龍一"),
        # A name ending in punctuation or a space ends there, whatever follows.
        ("M.I.A. Music", "M.I.A."),
        ("M.I.A.Music", "M.I.A."),
        ("Air Records", "Air "),
        # A name holding what a pattern would read as its own.
        ("Air (UK) Records", "Air (UK)"),
    ],
)
def test_a_label_starting_with_an_artists_whole_name_is_self_released_as_before(label: str, artist: str) -> None:
    assert determine_label_type(label, [(artist, "main")]) == "Self-Released"


@pytest.mark.parametrize(
    "artists",
    [[("Moby", "main"), ("Air", "main")], [("Air", "main"), ("Moby", "main")], [("Moby", "guest"), ("Air", "main")]],
)
def test_a_label_starting_with_any_main_artists_name_is_self_released_as_before(artists) -> None:
    assert determine_label_type("Air Records", artists) == "Self-Released"


@pytest.mark.parametrize(
    ("label", "artists"),
    [
        pytest.param("Air Records", [("Air", "guest")], id="a guest's name"),
        pytest.param("Air", [("Air Supply", "main")], id="the label shorter than the name"),
        pytest.param("Hot Air Records", AIR, id="the name later in the label"),
        pytest.param(None, AIR, id="no label"),
    ],
)
def test_other_labels_are_kept_as_before(label: str | None, artists: list[tuple[str, str]]) -> None:
    assert determine_label_type(label, artists) == label


@pytest.mark.parametrize("scraper", SCRAPERS)
def test_tidal_and_deezer_read_an_artists_name_by_the_same_rule(scraper) -> None:
    assert scraper().process_label({"label": "Airplane Records", "artists": AIR}) == "Airplane Records"


@pytest.mark.parametrize("scraper", SCRAPERS)
@pytest.mark.parametrize(
    ("label", "artists", "processed"),
    [
        ("Air Records", AIR, "Self-Released"),
        ("Virgin", AIR, "Virgin"),
        ("Air Records", [("Air", "guest")], "Air Records"),
        ("Air Records", [("Moby", "main"), ("Air", "main")], "Self-Released"),
        # Left to combine.py's rule: see process_label.
        ("Not On Label", AIR, "Not On Label"),
        (None, AIR, None),
    ],
)
def test_tidal_and_deezer_read_other_labels_as_before(scraper, label, artists, processed) -> None:
    assert scraper().process_label({"label": label, "artists": artists}) == processed
