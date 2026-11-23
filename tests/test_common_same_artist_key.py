"""same_artist_key (#1131): one key for an artist's name across a release, checked against judged pairs of names."""

import html
import tomllib
import unicodedata
from pathlib import Path

import pytest

from salmon.common.strings import comparable, same_artist_key

CASES = Path(__file__).parent / "fixtures" / "artist_credits"
PAIRS = tomllib.loads((Path(__file__).parent / "fixtures" / "name_pairs.toml").read_text(encoding="utf-8"))["pair"]
NAMES = sorted({name for pair in PAIRS for name in (pair["a"], pair["b"])})


def _id(pair: dict) -> str:
    return f"{pair['case']}: {pair['a']} / {pair['b']}"


def test_every_pair_is_judged_from_a_casebook_release() -> None:
    for pair in PAIRS:
        assert set(pair) <= {"case", "a", "b", "artists", "unsure", "joined_by", "why"}, _id(pair)
        assert pair["artists"] in {"one", "two", "part"}, _id(pair)
        assert ("joined_by" in pair) is (pair["artists"] == "one"), _id(pair)
        assert pair.get("joined_by", "key") in {"key", "Discogs", "MusicBrainz id", "nothing"}, _id(pair)
        assert isinstance(pair.get("unsure", False), bool), _id(pair)
        assert pair["why"], _id(pair)
        assert (CASES / f"{pair['case']}.toml").is_file(), _id(pair)


@pytest.mark.parametrize("pair", PAIRS, ids=_id)
def test_the_key_joins_one_artist_s_names_where_nothing_else_is_needed_and_never_two_artists(pair: dict) -> None:
    assert (same_artist_key(pair["a"]) == same_artist_key(pair["b"])) is (pair.get("joined_by") == "key")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Ahmad Jamal Trio", "The Ahmad Jamal Trio"),
        ("The-Dream", "The Dream"),
        ("The\u2013Dream", "The-Dream"),
        ("Dick Dale & His Del-Tones", "Dick Dale and His Del-Tones"),
        ("Me-and-You", "Me and You"),
        ("Kruder & Dorfmeister", "Kruder.Dorfmeister"),
        ("Simon and Garfunkel", "Simon & Garfunkel"),
        ("JAŸ-Z", "JAY Z"),
        ("The", "THE"),
        ("The&nbsp;Ahmad Jamal Trio", "Ahmad Jamal Trio"),
        ("Ｔｈｅ Ahmad Jamal Trio", "Ahmad Jamal Trio"),
        ("Thé Ahmad Jamal Trio", "Ahmad Jamal Trio"),
        ("The\u200b Ahmad Jamal Trio", "Ahmad Jamal Trio"),
        ('"The Ahmad Jamal Trio"', "Ahmad Jamal Trio"),
        ("The ", "THE"),
    ],
)
def test_one_artist_in_two_spellings_has_one_key(a: str, b: str) -> None:
    assert same_artist_key(a) == same_artist_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("!!!", "???"),
        ("The !!!", "!!!"),
        ("The The", "The Band"),
        ("And Also the Trees", "Also the Trees"),
        ("Stevie Nicks", "Stevie Wonder"),
        ("Carlos Santana", "Santana"),
        ('Richard "Humpty" Vission', "Richard Vission"),
        ('Willie "The Lion" Smith', "Willie Smith"),
        ("Tyler, The Creator", "Tyler"),
        ("Them", "M"),
    ],
)
def test_two_names_keep_two_keys(a: str, b: str) -> None:
    assert same_artist_key(a) != same_artist_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [("The Dream", "Dream"), ("The Prodigy", "Prodigy"), ("Peter and Gordon", "Peter Gordon")],
)
def test_two_artists_the_key_joins_and_salmon_shows(a: str, b: str) -> None:
    """The key's known cost: two artists whose names differ only by a leading "The" or a word "and" (the merge shows
    every such join, "Read as one artist")."""
    assert same_artist_key(a) == same_artist_key(b)


def _written_otherwise(name: str) -> list[str]:
    """The name as other sources may write it: decomposed, HTML-escaped (as trackers send it), padded, and with each
    space and hyphen as another of its kind."""
    spaces = ("\u00a0", "\u2009", "\u3000", "\t", "\u200b", "\u00ad")
    dashes = ("\u2010", "\u2011", "\u2013", "\u2014", "\u2212", "\ufe63", "\uff0d")
    return [
        unicodedata.normalize("NFD", name),
        html.escape(name),
        f" {name} ",
        "\u200b" + name,
        *(name.replace(" ", space) for space in spaces if " " in name),
        *(name.replace("-", dash) for dash in dashes if "-" in name),
    ]


@pytest.mark.parametrize("name", NAMES)
def test_a_name_written_otherwise_keeps_its_key(name: str) -> None:
    assert {same_artist_key(other) for other in _written_otherwise(name)} == {same_artist_key(name)}


def test_the_key_splits_only_a_glued_article_of_what_comparable_joins() -> None:
    """Every two names `comparable` joins, the key joins too, but "TheWeeknd" and "The Weeknd": "The" is dropped only
    before a space or dash, so "Theodore" keeps its letters."""
    names = set(NAMES) | {"TheWeeknd", "The Weeknd", "Theodore"}
    split = {
        frozenset((a, b))
        for a in names
        for b in names
        if a != b and comparable(a) and comparable(a) == comparable(b) and same_artist_key(a) != same_artist_key(b)
    }
    assert split == {frozenset(("TheWeeknd", "The Weeknd"))}
