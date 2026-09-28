from typing import TYPE_CHECKING, cast

import anyio
import pytest

from salmon.uploader import dupe_checker
from salmon.uploader.dupe_checker import (
    _prompt_for_recent_upload_results,
    _recent_upload_matches,
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
