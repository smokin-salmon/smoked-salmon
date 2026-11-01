from typing import TYPE_CHECKING, cast

import anyio
import asyncclick as click
import pytest

import salmon.uploader
from salmon.errors import RequestFailedError, SkipTracker
from salmon.uploader import dupe_checker
from salmon.uploader.dupe_checker import (
    _prompt_for_group_id,
    _prompt_for_recent_upload_results,
    _recent_upload_matches,
    _title_words,
    generate_dupe_check_searchstrs,
    print_search_results,
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


def test_the_recent_upload_prompt_offers_to_skip_the_tracker_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    texts: list[str] = []
    answers = iter(["s", "n", "s"])

    async def fake_prompt(text: str, *_args, **_kwargs) -> str:
        texts.append(text)
        return next(answers)

    monkeypatch.setattr(dupe_checker.click, "prompt", fake_prompt)
    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleSite()))

    # Not offered: "s" is no answer, and the prompt asks again.
    assert anyio.run(_prompt_for_recent_upload_results, gazelle_site, [], "some search", False) is None
    assert len(texts) == 2
    assert "[s]kip this tracker" not in texts[0]

    with pytest.raises(SkipTracker):
        anyio.run(_prompt_for_recent_upload_results, gazelle_site, [], "some search", False, True)
    assert "[s]kip this tracker" in texts[2]


def _answering(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Answer each prompt in turn; returns the prompts' texts."""
    asked: list[str] = []
    queue = list(answers)

    async def fake_prompt(text: str, *_args, **_kwargs) -> str:
        asked.append(text)
        return queue.pop(0)

    monkeypatch.setattr(dupe_checker.click, "prompt", fake_prompt)
    return asked


class LogSite(FakeGazelleSite):
    site_code = "RED"
    has_session_cookie = True

    def __init__(self) -> None:
        self.redirected: list[int] = []

    async def get_redirect_torrentgroupid(self, torrent_id: int) -> int:
        self.redirected.append(torrent_id)
        return 1000 + torrent_id


def test_0_with_no_recent_upload_listed_asks_again(monkeypatch) -> None:
    asked = _answering(monkeypatch, "0", "n")

    group_id = anyio.run(
        _prompt_for_recent_upload_results, cast("BaseGazelleApi", cast("object", LogSite())), [], "s", False
    )

    assert group_id is None
    assert len(asked) == 2


def test_the_group_prompt_offers_a_pick_only_when_groups_were_found(monkeypatch) -> None:
    asked = _answering(monkeypatch, "0", "n")

    group_id = anyio.run(_prompt_for_group_id, cast("BaseGazelleApi", cast("object", LogSite())), [], False)

    # 0 is no group, and with none found there is no first one to give: asked again.
    assert group_id is None
    assert len(asked) == 2
    assert "pick from groups found" not in asked[0]


def test_a_malformed_search_result_keeps_its_number_on_one_line(capsys) -> None:
    good = {"groupId": 1, "artist": "A", "groupName": "B", "groupYear": 2020, "releaseType": "Album", "tags": ["x"]}
    no_year = {"groupId": 2, "artist": "C", "groupName": "D", "releaseType": "Album", "tags": []}
    bad_tags = {**good, "groupId": 3, "tags": None}

    print_search_results(cast("BaseGazelleApi", cast("object", LogSite())), [good, no_year, bad_tags], "s")

    out = capsys.readouterr().out
    assert " 01 >> 1 | " in out
    assert " 02 >> This result could not be read | http://127.0.0.1/torrents.php?id=2\n" in out
    assert " 03 >> This result could not be read | http://127.0.0.1/torrents.php?id=3\n" in out


def test_a_remixes_album_keeps_remixes_in_its_search() -> None:
    # "Mix" removes an edition marker such as "(Original Mix)", not the word inside "(Remixes)".
    assert generate_dupe_check_searchstrs([("Artist", "main")], "Album (Remixes)") == ["artist album remixes"]


@pytest.mark.parametrize("upload_anyway", [True, False])
def test_an_unreadable_site_log_asks_before_uploading_without_the_check(
    monkeypatch: pytest.MonkeyPatch, capsys, upload_anyway: bool
) -> None:
    site = LogSite()
    asked: list[str] = []

    async def unreadable() -> list:
        raise RequestFailedError("Server error 503")

    async def answer(text: str, **kwargs) -> bool:
        asked.append(text)
        assert kwargs.get("default") is False
        return upload_anyway

    site.get_uploads_from_log = unreadable  # type: ignore[attr-defined]
    monkeypatch.setattr(salmon.uploader.interaction, "confirm", answer)

    if upload_anyway:
        anyio.run(salmon.uploader.last_min_dupe_check, site, ["artist album"], "Album")
    else:
        with pytest.raises(click.Abort):
            anyio.run(salmon.uploader.last_min_dupe_check, site, ["artist album"], "Album")

    out = capsys.readouterr().out
    assert "Could not read the RED site log: Server error 503" in out
    assert len(asked) == 1 and "Upload without the last-minute dupe check?" in asked[0]
