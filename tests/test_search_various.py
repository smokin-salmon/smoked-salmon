"""A store's result counts as various artists by its whole name, not by holding the word (#924)."""

from types import SimpleNamespace
from typing import Any

import pytest

from salmon.search import filter_results


def _results(artist: str) -> dict[str, Any]:
    return {"1": (SimpleNamespace(artist=artist, album="Hits"),)}


def test_an_artist_whose_name_holds_various_is_matched_by_name() -> None:
    results = _results("Various Cruelties")

    assert filter_results(results, ["Various Cruelties"], "Hits") == results


def test_an_artist_whose_name_holds_various_is_not_kept_for_other_artists() -> None:
    assert filter_results(_results("Various Producers"), ["Air", "Daft Punk"], "Hits") == {}


@pytest.mark.parametrize("artist", ["Various Artists", "Various"])
def test_a_various_artists_result_is_dropped_for_one_artist_and_kept_for_several_as_before(artist: str) -> None:
    results = _results(artist)

    assert filter_results(results, ["Air"], "Hits") == {}
    assert filter_results(results, ["Air", "Daft Punk"], "Hits") == results


@pytest.mark.parametrize("artist", ["various artists", "VARIOUS ARTISTS"])
def test_a_various_artists_result_in_any_case_is_kept_for_several(artist: str) -> None:
    results = _results(artist)

    assert filter_results(results, ["Air", "Daft Punk"], "Hits") == results
