"""#1188: a split name is joined in the decision, and only where one source claims every part. The files credit
Alice, Deezer states Bob and MusicBrainz guesses a guest "Alice & Bob": no one source claims both parts, so they stay
two mains, whatever runs after the decision (the merge's own release credits, `salmon up`'s second pass)."""

import random

import pytest
from helpers.tagger_combine import stores_stating
from test_common_credits import files_claim, store_claim
from test_tagger_combine_credits import decided, release, two_tracks

from salmon.common.credits import Claim
from salmon.tagger.combine import _join_split_names, _joins_each_source_completes, combine_metadatas
from salmon.tagger.metadata import _combine_scraped
from salmon.tagger.retagger import create_remixer_list
from salmon.tagger.sources.base import construct_replacement_list, fix_artists_list

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


def test_a_source_s_join_is_dropped_when_one_of_its_parts_goes_to_another_source_s_whole() -> None:
    """The files complete "Ava Max" and Deezer "Max Ray": Max goes to "Ava Max", the files' join coming first, so
    Deezer's join no longer has all its parts and is dropped. Ray stays Ray, and Max is in one name."""
    claims = [
        files_claim("Ava"),
        files_claim("Max"),
        store_claim("Deezer", "Max", "main", assumed=False),
        store_claim("Deezer", "Ray", "main", assumed=False),
    ]

    assert decided(claims, [("Ava Max", "composer"), ("Max Ray", "composer")]) == [("Ava Max", "main"), ("Ray", "main")]


def test_a_join_that_loses_its_part_only_to_a_dropped_join_stands() -> None:
    """Qobuz's longer "Ava Zzzzzzzz" takes Ava, so the files' "Ava Max" is dropped; Max then goes to no other whole,
    so Deezer's "Max Ray" holds both its parts and stands."""
    claims = [
        files_claim("Ava"),
        files_claim("Max"),
        store_claim("Deezer", "Max", "main", assumed=False),
        store_claim("Deezer", "Ray", "main", assumed=False),
        store_claim("Qobuz", "Ava", "main", assumed=False),
        store_claim("Qobuz", "Zzzzzzzz", "main", assumed=False),
    ]

    assert decided(claims, [(name, "composer") for name in ("Ava Max", "Max Ray", "Ava Zzzzzzzz")]) == [
        ("Ava Zzzzzzzz", "main"),
        ("Max Ray", "main"),
    ]


def test_two_sources_joins_that_end_in_one_name_both_stand() -> None:
    """The files complete "Ava Max" and Deezer, from Ava, Max and Ray, "Ava Max Ray": both joins end in "Ava Max Ray",
    so they share Ava and Max without a clash, and the track credits that one name."""
    claims = [
        files_claim("Ava"),
        files_claim("Max"),
        store_claim("Deezer", "Ava", "main", assumed=False),
        store_claim("Deezer", "Max", "main", assumed=False),
        store_claim("Deezer", "Ray", "main", assumed=False),
    ]

    assert decided(claims, [("Ava Max", "composer")], [("Ava Max Ray", "composer")]) == [("Ava Max Ray", "main")]


def test_a_join_is_kept_only_if_the_joins_before_it_still_take_all_their_parts() -> None:
    """Qobuz's longer "Ray Zedzedzed" takes Ray, so Deezer's "Ava Max Ray" is dropped. MusicBrainz's Ava and "Max
    Ray" would make that name too, but would take Ava from the files' "Ava Max" and leave Max in it: dropped as well,
    so Ava and Max stay one name."""
    claims = [
        store_claim("Qobuz", "Ray", "main", assumed=False),
        store_claim("Qobuz", "Zedzedzed", "main", assumed=False),
        files_claim("Ava"),
        files_claim("Max"),
        store_claim("Deezer", "Ava Max", "main", assumed=False),
        store_claim("Deezer", "Ray", "main", assumed=False),
        store_claim("MusicBrainz", "Ava", "main", assumed=False),
        store_claim("MusicBrainz", "Max Ray", "main", assumed=False),
    ]
    names = ("Ava Max", "Max Ray", "Ava Max Ray", "Ray Zedzedzed")

    assert decided(claims, [(name, "composer") for name in names]) == [
        ("Ray Zedzedzed", "main"),
        ("Ava Max", "main"),
        ("Max Ray", "main"),
    ]


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


# Zedzedzed is long, so a whole with it outranks the others and drops what overlaps it.
_ATOMS = ("Ava", "Max", "Ray", "Zedzedzed")
_NESTED = ("Ava Max", "Max Ray", "Ray Zedzedzed", "Ava Max Ray", "Max Ray Zedzedzed")


def _generated_kept_claims(count: int):
    """Each source claiming a random few of four names and the wholes they make, nested ones included, as remixers."""
    rng = random.Random(11883)
    for _ in range(count):
        sources = rng.sample(("files", "Deezer", "Qobuz", "MusicBrainz", "Tidal"), rng.randint(2, 5))
        yield [
            Claim(source, name, name, "remixer", assumed=False)
            for source in sources
            for name in (*_ATOMS, *_NESTED)
            if rng.random() < (0.5 if name in _ATOMS else 0.15)
        ]


def test_every_part_is_joined_by_a_join_that_takes_all_its_parts() -> None:
    """#1188: each name a part is joined into comes from one or more sources' joins that each take every one of their
    parts there, so no join gives an artist's parts two names, nor any part a name no whole join gives it."""
    to_replace = construct_replacement_list([(name, "composer") for name in (*_ATOMS, *_NESTED)])
    for claims in _generated_kept_claims(2000):
        joined = _joins_each_source_completes(claims, to_replace)
        joins = []
        for source in dict.fromkeys(claim.source for claim in claims):
            theirs = {}
            fix_artists_list(
                [(claim.name, claim.role) for claim in claims if claim.source == source], to_replace, theirs
            )
            for whole in dict.fromkeys(theirs.values()):
                joins.append((whole, [key for key, of in theirs.items() if of == whole]))
        for into in set(joined.values()):
            whole_joins = [
                parts
                for whole, parts in joins
                if joined.get((whole, "remixer"), whole) == into and all(joined.get(key) == into for key in parts)
            ]
            covered = {key for parts in whole_joins for key in parts}
            assert covered == {key for key, name in joined.items() if name == into}, ", ".join(
                f"{claim.source} {claim.name}" for claim in claims
            )
