"""[metadata.corrections] genres: a genre a store always gives wrong is replaced, or dropped, before the review
(#791)."""

from copy import deepcopy
from typing import Any

import msgspec
import pytest
from helpers.tagger_metadata_corrections import _scraped

from salmon import cfg
from salmon.config.validations import Corrections
from salmon.tagger.metadata import _combine_scraped
from salmon.tagger.sources.base import standardize_genres

ARTISTS = [("Dree Low", "main")]


@pytest.fixture
def corrections(monkeypatch):
    def set_genres(genres: dict[str, list[str]]) -> None:
        monkeypatch.setattr(cfg.metadata.corrections, "genres", genres)

    return set_genres


def _genres(store: list[str], files: list[str] | None = None) -> list[str]:
    """The genres the review gets when the store gives `store` and the files carry `files`."""
    # A scrape standardizes the store's genres; the files' own arrive as tagged.
    scraped = {**_scraped(ARTISTS), "genres": standardize_genres(store)}
    base = {**deepcopy(scraped), "genres": files if files is not None else list(store)}
    metadata: dict[str, Any] = _combine_scraped(["Qobuz"], [scraped], base, None)
    return metadata["genres"]


def test_a_genre_is_replaced_by_the_genres_it_names(corrections) -> None:
    corrections({"Free jazz & Avant-garde": ["Free Jazz", "Avant-Garde"]})

    assert _genres(["Jazz", "Free jazz & Avant-garde"]) == standardize_genres(["Jazz", "Free Jazz", "Avant-Garde"])


def test_an_empty_list_drops_the_genre(corrections) -> None:
    corrections({"Jazz contemporain": []})

    assert _genres(["Jazz", "Jazz contemporain"]) == standardize_genres(["Jazz"])


def test_a_correction_matches_in_any_case_and_past_outer_spaces(corrections) -> None:
    corrections({" jazz CONTEMPORAIN ": ["Jazz"]})

    assert _genres(["Jazz contemporain"]) == standardize_genres(["Jazz"])


def test_a_correction_does_not_match_a_genre_with_other_accents(corrections) -> None:
    corrections({"Chanson francaise": ["Chanson"]})

    assert _genres(["Chanson française"]) == standardize_genres(["Chanson française"])


def test_the_files_own_genres_are_corrected_too(corrections) -> None:
    corrections({"Jazz contemporain": ["Jazz"]})

    assert _genres(["Jazz"], files=["Jazz contemporain"]) == standardize_genres(["Jazz"])


def test_a_files_genre_is_matched_as_the_review_shows_it(corrections) -> None:
    # The files say "Electronica", which the review shows (and a store gives) as "Electronic".
    corrections({"Electronic": []})

    assert _genres(["Jazz"], files=["Electronica", "Jazz"]) == standardize_genres(["Jazz"])


def test_a_combination_is_dropped_without_the_genre_it_contains(corrections) -> None:
    corrections({"Pop & Rock": []})

    assert _genres(["Rock"], files=["Rock", "Pop & Rock"]) == standardize_genres(["Rock"])


def test_a_correction_is_applied_once_and_merges_duplicates(corrections) -> None:
    corrections({"Jazz contemporain": ["Jazz"], "Jazz": ["Big Band"], "Free jazz & Avant-garde": ["Jazz"]})

    assert _genres(["Jazz contemporain", "Free jazz & Avant-garde"]) == standardize_genres(["Jazz"])


def test_with_no_corrections_the_genres_are_unchanged(corrections) -> None:
    corrections({})

    assert _genres(["Jazz", "Jazz contemporain"]) == standardize_genres(["Jazz", "Jazz contemporain"])


@pytest.mark.parametrize("genres", [{" ": ["Jazz"]}, {"Jazz contemporain": ["Jazz", " "]}])
def test_a_correction_with_an_empty_genre_is_refused(genres: dict[str, list[str]]) -> None:
    with pytest.raises(ValueError, match="metadata.corrections.genres has an empty name"):
        Corrections(genres=genres)


def test_a_genre_corrected_twice_in_two_cases_is_refused() -> None:
    with pytest.raises(ValueError, match="names a genre twice"):
        Corrections(genres={"Jazz contemporain": ["Jazz"], "jazz Contemporain ": []})


def test_a_replacement_given_as_a_string_is_refused() -> None:
    with pytest.raises(msgspec.ValidationError, match="Expected `array`"):
        msgspec.convert({"genres": {"Jazz contemporain": "Jazz"}}, Corrections)


def test_dropping_a_genre_is_allowed() -> None:
    assert Corrections(genres={"Jazz contemporain": []}).genres == {"Jazz contemporain": []}
