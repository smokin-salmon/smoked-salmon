"""#1188: a split name is joined in the decision, and only where one source claims every part. The files credit
Alice, Deezer states Bob and MusicBrainz guesses a guest "Alice & Bob": no one source claims both parts, so they stay
two mains, whatever runs after the decision (the merge's own release credits, `salmon up`'s second pass)."""

import pytest
from helpers.tagger_combine import stores_stating
from test_common_credits import files_claim, store_claim
from test_tagger_combine_credits import decided, release, two_tracks

from salmon.tagger.combine import _join_split_names, combine_metadatas
from salmon.tagger.metadata import _combine_scraped
from salmon.tagger.retagger import create_remixer_list
from salmon.tagger.sources.base import construct_replacement_list

DECIDED = [("Alice", "main"), ("Bob", "main"), ("Alice & Bob", "guest")]


@pytest.fixture(autouse=True)
def _deezer_states_musicbrainz_assumes(monkeypatch: pytest.MonkeyPatch) -> None:
    stores_stating(monkeypatch, stating=("Deezer",), assuming=("MusicBrainz",))


def test_the_merge_keeps_two_sources_parts_apart_on_the_track_and_the_release() -> None:
    combined = combine_metadatas(
        ("Deezer", release(("Bob", "main"))),
        ("MusicBrainz", release(("Alice & Bob", "guest"))),
        base=release(("Alice", "main")),
    )

    assert combined["tracks"]["1"]["1"]["artists"] == DECIDED
    assert combined["artists"] == DECIDED


def test_salmon_up_s_second_pass_keeps_two_sources_parts_apart() -> None:
    combined = _combine_scraped(
        ["Deezer", "MusicBrainz"],
        [release(("Bob", "main")), release(("Alice & Bob", "guest"))],
        release(("Alice", "main")),
        None,
    )

    assert combined["tracks"]["1"]["1"]["artists"] == DECIDED
    assert combined["artists"] == DECIDED


def test_a_name_split_in_three_joins_only_the_parts_one_source_gives() -> None:
    """The files give Ava and Max, Deezer gives Ray: the files complete "Ava Max", and no source completes "Ava Max
    Ray"."""
    claims = [files_claim("Ava"), files_claim("Max"), store_claim("Deezer", "Ray", "main", assumed=False)]

    assert decided(claims, [("Ava Max", "composer")], [("Ava Max Ray", "composer")]) == [
        ("Ava Max", "main"),
        ("Ray", "main"),
    ]


def test_a_name_one_source_joins_follows_into_the_longer_name_another_source_completes_from_it() -> None:
    """The files join Ava and Max into "Ava Max"; Deezer gives "Ava Max" and Ray, every part of "Ava Max Ray". The
    files' claims, now of "Ava Max", follow it into the longer name, so the artist is credited once."""
    claims = [
        files_claim("Ava"),
        files_claim("Max"),
        store_claim("Deezer", "Ava Max", "main", assumed=False),
        store_claim("Deezer", "Ray", "main", assumed=False),
    ]

    assert decided(claims, [("Ava Max", "composer")], [("Ava Max Ray", "composer")]) == [("Ava Max Ray", "main")]


def test_the_longer_whole_is_chosen_once_each_whole_is_followed_into_longer_names() -> None:
    """The files complete "Ava Max", which Qobuz's "Ava Max" and Ray take on into "Ava Max Ray"; Deezer completes "Ava
    Carol" from Ava. Ava's longest whole is "Ava Max Ray", as Max's is: the files' two parts stay one name."""
    claims = [
        files_claim("Ava"),
        files_claim("Max"),
        store_claim("Deezer", "Ava", "main", assumed=False),
        store_claim("Deezer", "Carol", "main", assumed=False),
        store_claim("Qobuz", "Ava Max", "main", assumed=False),
        store_claim("Qobuz", "Ray", "main", assumed=False),
    ]
    to_replace = construct_replacement_list(
        [(name, "composer") for name in ("Ava", "Max", "Carol", "Ray", "Ava Max", "Ava Carol", "Ava Max Ray")]
    )

    joined = _join_split_names(claims, to_replace)

    assert [claim.name for claim in joined if claim.source == "files"] == ["Ava Max Ray", "Ava Max Ray"]


@pytest.mark.parametrize(
    ("deezer_remixers", "expected"),
    [(["Garfunkel"], ["Simon", "Garfunkel"]), (["Simon", "Garfunkel"], ["Simon and Garfunkel"])],
    ids=["one part from each source: two remixers", "both parts from one source: one remixer"],
)
def test_a_split_remixer_is_one_remixer_only_when_one_source_gives_both_parts(
    deezer_remixers: list[str], expected: list[str]
) -> None:
    files = two_tracks([("Bob", "main"), ("Simon", "remixer")], [("Simon and Garfunkel", "composer")])
    files["tracks"]["1"]["1"]["credited_remixers"] = ["Simon"]
    deezer = two_tracks([("Bob", "main"), *((name, "remixer") for name in deezer_remixers)], [])
    deezer["tracks"]["1"]["1"]["credited_remixers"] = deezer_remixers

    track = combine_metadatas(("Deezer", deezer), base=files)["tracks"]["1"]["1"]

    assert create_remixer_list(track) == expected
