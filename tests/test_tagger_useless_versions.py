"""A useless version is stripped from a title only as a whole trailing (…) or […], never as a word of the title."""

import pytest

from salmon import cfg
from salmon.tagger.pre_data import parse_title
from salmon.tagger.sources.base import MetadataMixin

KEPT = [
    "The Album (Lake Edition)",
    "Scrubbed Clean",
    "Not the Original",
    "Remastered",
    "Birds of a Feather Fall",
    "Album of the Mix Edition",
    "Night Songs (2011 Remastered)",
    "Night Songs (Feature Film)",
    "Night Songs (Remastered]",
    "Night Songs [Remastered)",
    "Night Songs\n(Remastered)",
    "Night Songs (\nClean)",
    "Night Songs (Deluxe\nEdition)",
    "Night Songs (Album\nMix)",
    "Night Songs (feat. Someone\nElse)",
    "(Remastered)",
]


@pytest.fixture(autouse=True)
def _strip_useless_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "strip_useless_versions", True)


@pytest.mark.parametrize(
    ("title", "parsed"),
    [
        ("The Album (Deluxe Edition)", ("The Album", "Deluxe Edition")),
        ("Night Songs [Remastered]", ("Night Songs", "Remastered")),
        ("Night Songs (Original Mix)", ("Night Songs", "Original Mix")),
        ("Night Songs (Album Mix)", ("Night Songs", "Album Mix")),
        ("Night Songs (feat. Someone Else)", ("Night Songs", "feat. Someone Else")),
        ("Night Songs ( Clean )", ("Night Songs", "Clean")),
    ],
)
def test_a_trailing_useless_version_is_the_edition(title: str, parsed: tuple[str, str]) -> None:
    found = parse_title(title)
    assert found == parsed


@pytest.mark.parametrize("title", KEPT)
def test_a_title_ending_in_a_junk_word_keeps_it(title: str) -> None:
    found = parse_title(title)
    assert found == (title, None)


@pytest.mark.parametrize("title", KEPT)
def test_a_track_title_ending_in_a_junk_word_keeps_it(title: str) -> None:
    found = MetadataMixin.parse_title(title, None)
    assert found == title


@pytest.mark.parametrize(
    ("title", "version", "parsed"),
    [
        ("Night Songs (Remastered)", None, "Night Songs"),
        ("Night Songs (feat. Someone Else)", None, "Night Songs"),
        ("Night Songs", "Live", "Night Songs (Live)"),
        ("Night Songs", "Remastered", "Night Songs"),
    ],
)
def test_a_track_title_loses_its_useless_version(title: str, version: str | None, parsed: str) -> None:
    found = MetadataMixin.parse_title(title, version)
    assert found == parsed


def test_nothing_is_stripped_when_the_option_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "strip_useless_versions", False)
    found = parse_title("The Album (Deluxe Edition)")
    assert found == ("The Album (Deluxe Edition)", None)
