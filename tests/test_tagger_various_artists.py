"""A "Various Artists" or "Various" credit names no artist: it is dropped from the release and its tracks (#831)."""

from copy import deepcopy
from typing import Any

import anyio
import pytest
from test_tagger_metadata_corrections import _scraped  # pyright: ignore[reportMissingImports]

import salmon.commands
from salmon.commands import build_description
from salmon.errors import InvalidMetadataError
from salmon.tagger import metadata_validator_base
from salmon.tagger.metadata import _combine_scraped, remove_various_artists
from salmon.tagger.review import _print_metadata
from salmon.tagger.sources.base import append_remixers_to_track_titles

URL = "https://www.qobuz.com/us-en/album/album/abc123"


def _combined(*tracks: list[tuple[str, str]]) -> dict[str, Any]:
    scraped = _scraped(*tracks)
    return _combine_scraped(["Qobuz"], [scraped], deepcopy(scraped), None)


@pytest.mark.parametrize("various", ["Various Artists", "Various", "VARIOUS", " various artists "])
def test_a_various_credit_is_dropped_from_the_tracks_and_the_release(various) -> None:
    metadata = _combined([("Dree Low", "main"), (various, "guest")])

    remove_various_artists(metadata)

    assert metadata["tracks"]["1"]["1"]["artists"] == [("Dree Low", "main")]
    assert metadata["artists"] == [("Dree Low", "main")]


def test_a_name_that_only_contains_various_is_kept() -> None:
    metadata = _combined([("Various Artists Collective", "main")])

    remove_various_artists(metadata)

    assert metadata["tracks"]["1"]["1"]["artists"] == [("Various Artists Collective", "main")]


def test_a_track_left_with_no_artist_goes_to_the_review_which_asks_for_one() -> None:
    metadata = _combined([("Various Artists", "main")], [("Dree Low", "main")])

    remove_various_artists(metadata)

    assert metadata["tracks"]["1"]["1"]["artists"] == []
    _print_metadata(metadata)
    with pytest.raises(InvalidMetadataError, match="at least one main artist per track"):
        metadata_validator_base(metadata)


def test_descgen_leaves_a_various_credit_out(monkeypatch) -> None:
    scraped = _scraped([("Dree Low", "main"), ("Various", "guest")])

    async def fake_run_metadata(url, return_source_name=False):
        return scraped, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)

    description = anyio.run(build_description, [URL])

    assert "Dree Low" in description
    assert "Various" not in description


@pytest.mark.parametrize(
    ("remixers", "title"),
    [(["Various"], "Song"), (["Owen", "Various Artists"], "Song (Owen Remix)")],
    ids=["alone", "beside"],
)
def test_a_various_remixer_is_not_put_in_the_title(remixers, title) -> None:
    tracks = {"1": {"1": {"title": "Song", "artists": [("Dree Low", "main"), *((r, "remixer") for r in remixers)]}}}

    append_remixers_to_track_titles(tracks)
    assert tracks["1"]["1"]["title"] == title
