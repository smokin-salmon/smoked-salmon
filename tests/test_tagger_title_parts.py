"""The pieces a track's title is written from (#869 step 3b): a version's brackets, a title folded with its version,
and accents a store restores."""

import msgspec
import pytest

from salmon import cfg
from salmon.common.strings import enclosed, restore_accents, split_version, unbracketed
from salmon.tagger.sources.base import folded_title, title_with_version


@pytest.mark.parametrize(
    ("text", "expected"),
    [("(Mixed)", True), ("(Album Version (Explicit))", True), ("[Live]", True), ("(A) (B)", False), ("(A", False)],
)
def test_one_pair_of_brackets_encloses_all_of_a_text_or_not(text: str, expected: bool) -> None:
    assert enclosed(text) is expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("(Mixed)", "Mixed"),
        ("(Album Version (Explicit))", "Album Version (Explicit)"),
        ("Album Version (Explicit)", "Album Version (Explicit)"),
        ("(A) (B)", "(A) (B)"),
        (" ", None),
        (None, None),
    ],
)
def test_a_version_loses_only_brackets_enclosing_all_of_it(given: str | None, expected: str | None) -> None:
    assert unbracketed(given) == expected


@pytest.mark.parametrize(
    ("folded", "title", "expected"),
    [
        ("Spellbound (Mixed)", "Spellbound", ("Spellbound", "Mixed")),
        ("Spellbound [Mixed]", "Spellbound", ("Spellbound", "Mixed")),
        (
            "Livin' Free (Soundtrack Mix) (Mixed)",
            "Livin' Free (Soundtrack Mix)",
            ("Livin' Free (Soundtrack Mix)", "Mixed"),
        ),
        ("Livin' Free (Soundtrack Mix) (Mixed)", "Livin' Free", None),
        (
            "Livin’ Free (Soundtrack Mix) (Mixed)",
            "Livin' Free (Soundtrack Mix)",
            ("Livin’ Free (Soundtrack Mix)", "Mixed"),
        ),
        ("Song (X Remix)", "Song", ("Song", "X Remix")),
        ("Song", "Song", None),
    ],
)
def test_a_folded_title_splits_into_a_title_and_its_version(
    folded: str, title: str, expected: tuple[str, str] | None
) -> None:
    assert split_version(folded, title) == expected


@pytest.mark.parametrize(
    ("title", "accented", "expected"),
    [
        ("Ny Batteri", "Ný batterí", "Ný Batterí"),
        ("CAFE", "Café", "CAFÉ"),
        ("JOHN", "ǰohn", "JOHN"),  # "ǰ" has no one-letter capital: the title's letter stays
        ("ハロー", "バロー", "ハロー"),  # a kana's voicing mark is not an accent
        ("AGAETIS BYRJUN", "Ágætis byrjun", "AGAETIS BYRJUN"),  # "æ" against "ae": a length apart, left as it is
        ("Cafe", "Cafe\u0301", "Café"),  # the store's accent decomposed (NFD)
        ("That's The Way", "That’s the Way", "That's The Way"),  # punctuation stays the files'
        ("Papa", "Pаpа", "Papa"),  # a Cyrillic "а" is a look-alike, not an accent
        ("Lodz", "Łódź", "Łódź"),  # a letter with a stroke, which decomposes to no plain letter
        ("OJEBLIKKET", "Øjeblikket", "ØJEBLIKKET"),
        ("Ny\u0301 Batteri", "Ný batterí", "Ný Batterí"),  # the files' accent decomposed (NFD)
        ("Cafe\u0301", "Café", "Cafe\u0301"),  # nothing to restore: the files' text as it is
        ("Song", "Other", "Song"),
    ],
)
def test_accents_a_store_gives_are_restored_letter_by_letter(title: str, accented: str, expected: str) -> None:
    assert restore_accents(title, accented) == expected


def test_a_folded_title_is_the_title_and_its_version_as_a_store_shows_them() -> None:
    assert folded_title({"title": "Cold Heart", "version": "PNAU Remix"}) == "Cold Heart (PNAU Remix)"
    assert folded_title({"title": "Cold Heart"}) == "Cold Heart"


def test_a_version_holding_brackets_of_its_own_keeps_them(monkeypatch: pytest.MonkeyPatch) -> None:
    formatting = msgspec.structs.replace(cfg.upload.formatting, strip_useless_versions=True)
    monkeypatch.setattr(cfg.upload, "formatting", formatting)

    assert title_with_version("Dark Fantasy", "Album Version (Explicit)") == "Dark Fantasy (Album Version (Explicit))"
    assert title_with_version("Song", "(A) (B)") == "Song (A) (B)"
    assert title_with_version("Song", "(A) B") == "Song ((A) B)"
    assert title_with_version("Alive", "Live") == "Alive (Live)"
    assert title_with_version("Song (Remastered 2011)", "Remaster") == "Song (Remastered 2011)"
    assert title_with_version("Song - Live Version", "Live Version") == "Song - Live Version"


@pytest.mark.parametrize(
    ("title", "version"), [("Live and Let Die", "Live"), ("Demolition Man", "Demo"), ("Born to Run", "Run")]
)
def test_a_version_a_title_s_own_words_start_with_is_still_added(title: str, version: str) -> None:
    """Only the title's version part, from its first bracket or dash, can already say the version."""
    assert title_with_version(title, version) == f"{title} ({version})"
    assert title_with_version("Dark Fantasy (Album Version (Explicit))", "Album Version (Explicit)") == (
        "Dark Fantasy (Album Version (Explicit))"
    )
