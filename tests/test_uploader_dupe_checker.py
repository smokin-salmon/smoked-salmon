from typing import TYPE_CHECKING, cast

import anyio
import pytest

from salmon.uploader import dupe_checker
from salmon.uploader.dupe_checker import (
    _prompt_for_recent_upload_results,
    _recent_upload_matches,
    _title_words,
    generate_dupe_check_searchstrs,
)

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


class FakeGazelleSite:
    site_string = "RED"
    base_url = "http://127.0.0.1"


def test_recent_upload_prompt_keeps_master_wording_when_there_are_no_recent_uploads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the group search found nothing and the site log has no recent uploads either, the
    "similar recent uploads" header is misleading: there is nothing to pick from. Master's plain
    "Would you like to upload to an existing group?" wording should be shown instead (#518 follow-up).
    """
    prompt_text = ""

    async def fake_prompt(text: str, *_args, **_kwargs) -> str:
        nonlocal prompt_text
        prompt_text = text
        return "n"

    monkeypatch.setattr(dupe_checker.click, "prompt", fake_prompt)

    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleSite()))
    anyio.run(_prompt_for_recent_upload_results, gazelle_site, [], "some search", True)

    assert "Would you like to upload to an existing group?" in prompt_text
    assert "similar recent uploads" not in prompt_text
    assert "not exact group matches" not in prompt_text
    out = capsys.readouterr().out
    assert "Found similar recent uploads" not in out


def test_recent_upload_match_requires_more_than_shared_artist_prefix() -> None:
    searchstrs = generate_dupe_check_searchstrs([["Anna Zak", "main"], ["אביב גפן", "main"]], "מה נשאר לי ממך")
    comparisons = generate_dupe_check_searchstrs([["Anna Zak", "main"]], "קלטתי אותך")

    assert _recent_upload_matches(searchstrs, comparisons, tolerance=0.5) is False


def test_recent_upload_match_uses_all_generated_search_strings() -> None:
    searchstrs = generate_dupe_check_searchstrs([["Anna Zak", "main"], ["אביב גפן", "main"]], "מה נשאר לי ממך")
    comparisons = generate_dupe_check_searchstrs([["אביב גפן", "main"]], "מה נשאר לי ממך")

    assert _recent_upload_matches(searchstrs, comparisons, tolerance=0.5) is True


def test_recent_upload_match_accepts_true_collab_title_match() -> None:
    searchstrs = generate_dupe_check_searchstrs([["Anna Zak", "main"], ["אביב גפן", "main"]], "מה נשאר לי ממך")
    comparisons = generate_dupe_check_searchstrs([["Anna Zak & אביב גפן", "main"]], "מה נשאר לי ממך")

    assert _recent_upload_matches(searchstrs, comparisons, tolerance=0.5) is True


def test_recent_upload_match_requires_shared_title_word_when_titles_given() -> None:
    """A shared three-word artist alone can carry a high ratio and word-overlap fraction even with
    a completely different one-word title. Passing normalized title words for both sides requires
    the titles to share a word too (#518 CodeRabbit follow-up on #509)."""
    searchstrs = generate_dupe_check_searchstrs([["John James Smith", "main"]], "Sunrise")
    comparisons = generate_dupe_check_searchstrs([["John James Smith", "main"]], "Sunset")

    assert (
        _recent_upload_matches(
            searchstrs,
            comparisons,
            tolerance=0.5,
            our_title_words=_title_words("Sunrise"),
            candidate_title_words=_title_words("Sunset"),
        )
        is False
    )
    # Without title words, the shared artist alone is still enough (the pre-existing behaviour).
    assert _recent_upload_matches(searchstrs, comparisons, tolerance=0.5) is True


def test_an_html_escaped_edition_is_the_same_edition() -> None:
    """RED sends HTML-escaped text: "Collector&#39;s Edition" is the edition "Collector's Edition"."""
    torrent = {
        "media": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "remastered": True,
        "remasterYear": 2024,
        "remasterTitle": "Collector&#39;s Edition",
        "remasterCatalogueNumber": "R&amp;B-01",
    }
    group = {"group": {"year": 2020, "catalogueNumber": ""}, "torrents": [torrent]}
    release = {
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "year": "2024",
        "edition_title": "Collector's Edition",
        "catno": "R&B-01",
    }

    assert dupe_checker.matching_torrents(group, release) == [torrent]
    assert dupe_checker._catno_note(torrent, group, release) == ""


@pytest.mark.parametrize(("ours", "logged"), [("Rock 'n' Roll", "Rock'n'Roll"), ("Lovin’", "Lovin'")])
def test_title_words_ignore_punctuation(ours: str, logged: str) -> None:
    assert _title_words(ours) == _title_words(logged)


_HELD_RELEASE = {"source": "WEB", "format": "FLAC", "encoding": "24bit Lossless", "year": "2024"}
_HELD_FLAC = {
    "id": 1,
    "media": "WEB",
    "format": "FLAC",
    "encoding": "24bit Lossless",
    "remasterYear": 2024,
    "remasterRecordLabel": "Label",
    "remasterCatalogueNumber": "0000000000017",
}


def test_held_formats_count_only_the_edition_the_site_filed_the_flac_under() -> None:
    flac = _HELD_FLAC
    other = {
        **flac,
        "id": 2,
        "encoding": "Lossless",
        "remasterTitle": "Extended Mix",
        "remasterCatalogueNumber": "CAT-2",
    }
    same = {**flac, "id": 3, "format": "MP3", "encoding": "320"}
    titled = {**flac, "id": 4, "format": "MP3", "encoding": "V0 (VBR)", "remasterTitle": "Extended Mix"}
    group = {"group": {"year": 2024}, "torrents": [flac, other, same, titled]}
    formats = {"16bit": ("FLAC", "Lossless"), "MP3 320": ("MP3", "320"), "MP3 V0": ("MP3", "V0 (VBR)")}

    filed = dupe_checker.held_formats(group, _HELD_RELEASE, flac, formats, uploaded=True)
    # Not in the group, or not sent by this run: the transcodes' edition is unknown, so every match counts.
    unfiled = dupe_checker.held_formats(group, _HELD_RELEASE, {"id": 99}, formats, uploaded=True)
    existing = dupe_checker.held_formats(group, _HELD_RELEASE, flac, formats)

    assert filed == {"MP3 320"}
    assert unfiled == existing == {"16bit", "MP3 320", "MP3 V0"}


@pytest.mark.parametrize(
    ("flac_fields", "mp3_fields", "held"),
    [
        # One catalogue number, typed two ways: the same release.
        ({}, {"remasterCatalogueNumber": "CAT2"}, {"MP3 320"}),
        ({}, {"remasterCatalogueNumber": "cat-2"}, {"MP3 320"}),
        ({}, {"remasterCatalogueNumber": "CAT3"}, set()),
        # An id sent as text is the same torrent.
        ({"id": "1"}, {"remasterCatalogueNumber": "CAT3"}, set()),
        # An original release has the group's edition: the same year, label and catalogue number as the FLAC's.
        (
            {},
            {"remastered": False, "remasterYear": 0, "remasterRecordLabel": "", "remasterCatalogueNumber": ""},
            {"MP3 320"},
        ),
        (
            {"remasterCatalogueNumber": "CAT3"},
            {"remastered": False, "remasterYear": 0, "remasterRecordLabel": "", "remasterCatalogueNumber": ""},
            set(),
        ),
    ],
)
def test_held_formats_tell_editions_apart_by_their_fields(flac_fields, mp3_fields, held) -> None:
    flac = {**_HELD_FLAC, "remasterCatalogueNumber": "CAT-2", **flac_fields}
    mp3 = {**flac, "id": 2, "format": "MP3", "encoding": "320", **mp3_fields}
    group = {"group": {"year": 2024, "recordLabel": "Label", "catalogueNumber": "CAT-2"}, "torrents": [flac, mp3]}

    found = dupe_checker.held_formats(group, _HELD_RELEASE, {"id": 1}, {"MP3 320": ("MP3", "320")}, uploaded=True)

    assert found == held
