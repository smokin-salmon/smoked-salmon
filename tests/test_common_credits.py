"""The credit types and resolve_credits (ADR 0007): the rules, each claim's reason, and the types against the code
that builds the metadata they describe."""

from typing import get_args

import pytest
from test_artist_credit_casebook import CASES, _record, files, parse

from salmon.common.credits import Claim, ReleaseMetadata, Role, Source, TrackMetadata, resolve_credits
from salmon.constants import ARTIST_IMPORTANCES
from salmon.tagger.pre_data import EMPTY_METADATA
from salmon.tagger.sources import METASOURCES


def files_claim(name: str, role: Role | None = "main") -> Claim:
    return Claim("files", name, name, role, assumed=False)


def store_claim(source: Source, name: str, role: Role, assumed: bool = True) -> Claim:
    return Claim(source, name, name, role, assumed=assumed)


def resolved(*claims: Claim) -> list[tuple[tuple[str, str], str | None, tuple[str, ...], tuple[str, ...]]]:
    """The one track's credits, each with its reason's rule, sources and disputing sources."""
    return [
        (credit, reason.rule, reason.from_sources, reason.disputed_by)
        for credit, reason in resolve_credits({"1": claims})["1"]
    ]


def test_the_roles_are_the_eight_salmon_ranks() -> None:
    assert set(get_args(Role)) == set(ARTIST_IMPORTANCES)


def test_the_sources_are_the_stores_and_the_files() -> None:
    assert set(get_args(Source)) == {*METASOURCES, "files"}


def test_an_assumed_claim_needs_a_role() -> None:
    with pytest.raises(ValueError, match="needs a role"):
        Claim("Tidal", "Alice", "Alice", None, assumed=True)


def test_a_stated_role_beats_an_assumed_one() -> None:
    claims = (store_claim("Tidal", "Alice", "main"), store_claim("Qobuz", "Alice", "guest", assumed=False))

    assert resolved(*claims) == [(("Alice", "guest"), "stated over assumed", ("Qobuz",), ("Tidal",))]


def test_the_files_stated_main_beats_the_stores_assumed_guest() -> None:
    claims = (files_claim("Alice"), store_claim("Deezer", "Alice", "guest"), store_claim("Qobuz", "Alice", "guest"))

    assert resolved(*claims) == [(("Alice", "main"), "stated over assumed", ("files",), ("Deezer", "Qobuz"))]


def test_otherwise_the_first_claim_wins() -> None:
    claims = (store_claim("Tidal", "Alice", "main"), store_claim("Deezer", "Alice", "guest"))

    assert resolved(*claims) == [(("Alice", "main"), "first", ("Tidal",), ("Deezer",))]


def test_a_credit_no_source_disputes_has_no_rule() -> None:
    claims = (files_claim("Alice"), store_claim("Deezer", "Alice", "main"))

    assert resolved(*claims) == [(("Alice", "main"), None, ("files", "Deezer"), ())]


def test_a_source_that_gives_both_roles_disputes_nothing() -> None:
    assert resolved(files_claim("Bob"), files_claim("Bob", "guest")) == [(("Bob", "main"), None, ("files",), ())]


def test_roles_other_than_main_and_guest_are_added_together() -> None:
    claims = (files_claim("Alice"), store_claim("Deezer", "Alice", "remixer"), store_claim("Qobuz", "Bob", "composer"))

    assert [credit for credit, *_ in resolved(*claims)] == [
        ("Alice", "main"),
        ("Alice", "remixer"),
        ("Bob", "composer"),
    ]


def test_an_unmapped_role_is_left_out() -> None:
    assert resolved(files_claim("Alice"), files_claim("Bob", None)) == [(("Alice", "main"), None, ("files",), ())]


def test_credits_keep_the_order_their_names_were_first_claimed_in() -> None:
    claims = (
        store_claim("Tidal", "Bob", "main"),
        files_claim("Alice"),
        store_claim("Qobuz", "Bob", "guest", assumed=False),
    )

    assert [credit for credit, *_ in resolved(*claims)] == [("Bob", "guest"), ("Alice", "main")]


def test_names_are_matched_as_given() -> None:
    assert [credit for credit, *_ in resolved(files_claim("Kool & the Gang"), files_claim("Kool & The Gang"))] == [
        ("Kool & the Gang", "main"),
        ("Kool & The Gang", "main"),
    ]


def test_the_types_name_the_keys_salmon_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every key the stores' parses and the files' reader build, and only those, with the keys all of them set
    required."""
    record = _record(CASES / "getz-gilberto.toml")
    releases = [*(parse(record, store, monkeypatch) for store in ("deezer", "qobuz", "discogs", "musicbrainz"))]
    releases.append(files(record["track"]))
    tracks = [track for release in releases for disc in release["tracks"].values() for track in disc.values()]

    track_keys = TrackMetadata.__required_keys__ | TrackMetadata.__optional_keys__
    assert track_keys == {key for track in tracks for key in track} | {"artist_variations"}
    assert TrackMetadata.__required_keys__ == set.intersection(*(set(track) for track in tracks))

    release_keys = ReleaseMetadata.__required_keys__ | ReleaseMetadata.__optional_keys__
    assert release_keys == {key for release in releases for key in release} | {*EMPTY_METADATA, "tags"}
    assert ReleaseMetadata.__required_keys__ == set.intersection(*(set(release) for release in releases))
