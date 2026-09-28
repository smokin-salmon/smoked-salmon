"""Public-path regression test for #509.

Only imports names that exist on master (dupe_check_recent_torrents,
generate_dupe_check_searchstrs), never the private _recent_upload_matches helper. This drives
dupe_check_recent_torrents itself, so it demonstrates the behaviour difference against master's
actual comparison logic instead of failing on an ImportError for a helper master does not have.
"""

from typing import TYPE_CHECKING, cast

import anyio

from salmon.uploader.dupe_checker import dupe_check_recent_torrents, generate_dupe_check_searchstrs

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


class FakeGazelleSite:
    """A minimal stand-in for BaseGazelleApi that only serves get_uploads_from_log()."""

    def __init__(self, uploads: list[tuple]) -> None:
        self._uploads = uploads

    async def get_uploads_from_log(self) -> list[tuple]:
        return self._uploads


def test_dupe_check_recent_torrents_ignores_shared_artist_prefix_only() -> None:
    """A logged upload that shares only an artist with our release must not be flagged as a dupe,
    a true collab title match must still be flagged, and a match that only shows up through a
    search string other than searchstrs[0] must also be flagged (master only checks searchstrs[0]).
    """
    searchstrs = generate_dupe_check_searchstrs([["Anna Zak", "main"], ["אביב גפן", "main"]], "מה נשאר לי ממך")
    false_positive_upload = (1, "Anna Zak", "קלטתי אותך")
    true_match_upload = (2, "Anna Zak & אביב גפן", "מה נשאר לי ממך")
    # Matches only through searchstrs[1] ("אביב גפן" + album), not searchstrs[0] ("Anna Zak" + album).
    second_artist_only_match_upload = (3, "אביב גפן", "מה נשאר לי ממך")
    gazelle_site = cast(
        "BaseGazelleApi",
        cast("object", FakeGazelleSite([false_positive_upload, true_match_upload, second_artist_only_match_upload])),
    )

    hits = anyio.run(dupe_check_recent_torrents, gazelle_site, searchstrs)

    assert false_positive_upload not in hits
    assert true_match_upload in hits
    assert second_artist_only_match_upload in hits


def test_dupe_check_recent_torrents_requires_shared_title_word_not_just_shared_artist() -> None:
    """A three-word shared artist can carry a high SequenceMatcher ratio and word-overlap fraction
    on its own, even with a completely different one-word title ("john james smith sunrise" vs
    "john james smith sunset" share 3 of 4 words). Passing our release's title makes
    dupe_check_recent_torrents require the titles themselves to share a word, while a same-title
    match (including a collab) still hits (#518 CodeRabbit follow-up).
    """
    artist = [["John James Smith", "main"]]
    our_title = "Sunrise"
    searchstrs = generate_dupe_check_searchstrs(artist, our_title)
    different_title_upload = (1, "John James Smith", "Sunset")
    same_title_upload = (2, "John James Smith", "Sunrise")
    collab_title_upload = (3, "John James Smith & Someone Else", "Sunrise")
    gazelle_site = cast(
        "BaseGazelleApi",
        cast("object", FakeGazelleSite([different_title_upload, same_title_upload, collab_title_upload])),
    )

    hits = anyio.run(dupe_check_recent_torrents, gazelle_site, searchstrs, our_title)

    assert different_title_upload not in hits
    assert same_title_upload in hits
    assert collab_title_upload in hits
