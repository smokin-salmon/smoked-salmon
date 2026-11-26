"""The search hints salmon gathers from the files' tags for MusicBrainz's fielded release search (#1178)."""

from types import SimpleNamespace
from typing import Any

import pytest

from salmon.search.hints import SearchHints, search_hints


def _tags(*files: dict[str, Any]) -> dict[str, SimpleNamespace]:
    fields = ("albumartist", "artist", "album", "date")
    return {f"{n:02d}.flac": SimpleNamespace(**{k: f.get(k) for k in fields}) for n, f in enumerate(files, 1)}


def test_the_album_artist_beats_the_artist() -> None:
    file = {
        "albumartist": "Miles Davis",
        "artist": ["Miles Davis", "John Coltrane"],
        "album": "Kind of Blue",
        "date": "1959-08-17",
    }
    assert search_hints(_tags(file, file)) == SearchHints("Miles Davis", "Kind of Blue", 2, "1959")


def test_without_an_album_artist_the_artist_is_used_joined() -> None:
    file = {"artist": ["A", "B"], "album": "X"}
    assert search_hints(_tags(file, file)).artist == "A; B"


def test_no_artist_tags_give_no_artist() -> None:
    assert search_hints(_tags({"album": "X"}, {"album": "X"})).artist is None


def test_files_without_artist_tags_do_not_outvote_the_tagged_one() -> None:
    files = [
        {"albumartist": "Miles Davis", "album": "Kind of Blue"},
        {"album": "Kind of Blue"},
        {"album": "Kind of Blue"},
    ]
    assert search_hints(_tags(*files)).artist == "Miles Davis"


def test_an_explicit_various_artists_credit_is_kept() -> None:
    files = [{"albumartist": "Various Artists"}, {"albumartist": "Various Artists"}, {"artist": "Someone"}]
    assert search_hints(_tags(*files)).artist == "Various Artists"


def test_the_most_common_artist_wins_grouped_case_and_punctuation_blind() -> None:
    files = [{"albumartist": "AC/DC"}, {"albumartist": "Foo"}, {"albumartist": "ac dc"}]
    files += [{"albumartist": "AC/DC"}, {"albumartist": "Foo"}]
    assert search_hints(_tags(*files)).artist == "AC/DC"


def test_an_underscore_is_punctuation_to_the_artist_grouping() -> None:
    files = [{"albumartist": "Foo_Bar"}] * 2 + [{"albumartist": "Foo Bar"}] * 2 + [{"albumartist": "Other"}] * 3
    assert search_hints(_tags(*files)).artist in {"Foo_Bar", "Foo Bar"}


def test_the_most_common_album_wins_and_none_when_absent() -> None:
    assert search_hints(_tags({"album": "A"}, {"album": "B"}, {"album": "B"})).album == "B"
    assert search_hints(_tags({}, {})).album is None


def test_track_count_is_the_file_count() -> None:
    assert search_hints(_tags({}, {}, {})).track_count == 3


@pytest.mark.parametrize(
    ("dates", "year"),
    [
        (["1959-08-17"] * 3, "1959"),
        (["1959", "1959", None], "1959"),
        (["1959", None, None, None], None),
        (["1959", "1959", None, None], None),
        (["1959", "1960", "1959"], None),
        (["2019; 2020"] * 2, "2019"),
        (["c. 1959"] * 2, None),
        (["", ""], None),
    ],
)
def test_the_year_needs_a_majority_that_agrees(dates: list[str | None], year: str | None) -> None:
    assert search_hints(_tags(*({"date": d} for d in dates))).year == year


def test_a_blank_tag_is_no_tag() -> None:
    blank = {"albumartist": " ", "artist": [" "], "album": "  ", "date": " "}
    assert search_hints(_tags(blank, blank)) == SearchHints(None, None, 2, None)


def test_a_zero_year_is_no_year() -> None:
    assert search_hints(_tags({"date": "0000"}, {"date": "0000-00-00"})).year is None
    assert search_hints(_tags({"date": "0000"}, {"date": "0000"})).year is None


def test_overwrite_keeps_only_the_album_and_the_track_count() -> None:
    file = {"albumartist": "Miles Davis", "artist": ["Miles Davis"], "album": "Kind of Blue", "date": "1959"}
    assert search_hints(_tags(file, file), overwrite=True) == SearchHints(None, "Kind of Blue", 2, None)


def test_a_file_without_tags_gives_no_hints_from_it() -> None:
    assert search_hints({"01.flac": None, "02.flac": {}}) == SearchHints(None, None, 2, None)
