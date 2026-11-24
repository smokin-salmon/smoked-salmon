"""The merge's last cleaning of each track's credits (`clean_metadata`): a main also credited as the track's guest or
remixer, or on a classical release its composer or conductor, loses main. The remixer and guest check, as before; and
on a classical release the files' classical rule, applied to the decided credits, so a store's main can't undo it
(ADR 0007, decision 3). A scrape used alone runs the same rule (#1183)."""

import random
from copy import deepcopy

from helpers.tagger_combine import stating, stores_stating
from test_tagger_combine_credits import release

from salmon.tagger.combine import combine_metadatas, decide_alone
from salmon.tagger.metadata import _mains_to_drop, clean_metadata
from salmon.tagger.sources.base import generate_artists


def cleaned(artists: list[tuple[str, str]], genres: list[str] | None = None) -> list[tuple[str, str]]:
    meta = release()
    meta["genres"] = genres or []
    meta["tracks"]["1"]["1"]["artists"] = list(artists)
    return clean_metadata(meta)["tracks"]["1"]["1"]["artists"]


CLASSICAL = ["Classical"]


# The remixer and guest check, as before


def test_as_before_a_main_who_remixes_the_track_loses_main_beside_another_main() -> None:
    got = cleaned([("Dua Lipa", "main"), ("Madonna", "main"), ("Madonna", "remixer")])

    assert got == [
        ("Dua Lipa", "main"),
        ("Madonna", "remixer"),
    ]


def test_as_before_a_main_who_remixes_their_own_track_keeps_main() -> None:
    got = cleaned([("Alice", "main"), ("Alice", "remixer")])

    assert got == [("Alice", "main"), ("Alice", "remixer")]


def test_as_before_mains_who_are_all_guests_lose_main_until_one_is_left() -> None:
    artists = [("Alice", "main"), ("Bob", "main"), ("Alice", "guest"), ("Bob", "guest")]

    got = cleaned(artists)

    assert got == [("Bob", "main"), ("Alice", "guest"), ("Bob", "guest")]


def test_as_before_a_composer_stays_main_off_classical() -> None:
    artists = [("Alice", "main"), ("Bob", "main"), ("Bob", "composer")]

    got = cleaned(artists, ["Pop"])

    assert got == artists


# The classical rule


def test_a_composer_a_store_states_as_main_loses_main_on_a_classical_release() -> None:
    artists = [("Giacomo Puccini", "main"), ("Berliner Philharmoniker", "main"), ("Giacomo Puccini", "composer")]

    got = cleaned(artists, CLASSICAL)

    assert got == [("Berliner Philharmoniker", "main"), ("Giacomo Puccini", "composer")]


def test_the_performers_keep_main_whatever_their_place_beside_the_composer() -> None:
    artists = [
        ("Mirella Freni", "main"),
        ("Giacomo Puccini", "main"),
        ("Luciano Pavarotti", "main"),
        ("Giacomo Puccini", "composer"),
    ]

    got = cleaned(artists, CLASSICAL)

    assert got == [
        ("Mirella Freni", "main"),
        ("Luciano Pavarotti", "main"),
        ("Giacomo Puccini", "composer"),
    ]


def test_a_conductor_is_credited_as_conductor_not_main_on_a_classical_release() -> None:
    artists = [("Fritz Reiner", "main"), ("Chicago Symphony Orchestra", "main"), ("Fritz Reiner", "conductor")]

    got = cleaned(artists, CLASSICAL)

    assert got == [("Chicago Symphony Orchestra", "main"), ("Fritz Reiner", "conductor")]


def test_a_composer_who_conducts_is_composer_and_conductor_not_main() -> None:
    artists = [
        ("Leonard Bernstein", "main"),
        ("Kiri Te Kanawa", "main"),
        ("Leonard Bernstein", "composer"),
        ("Leonard Bernstein", "conductor"),
    ]

    got = cleaned(artists, CLASSICAL)

    assert got == [
        ("Kiri Te Kanawa", "main"),
        ("Leonard Bernstein", "composer"),
        ("Leonard Bernstein", "conductor"),
    ]


def test_as_before_a_composer_who_is_every_main_keeps_main() -> None:
    """A track is never left without a main: the composer playing his own works alone."""
    artists = [("Alice", "main"), ("Alice", "composer")]

    got = cleaned(artists, CLASSICAL)

    assert got == artists


def test_a_classical_genre_in_another_language_counts() -> None:
    artists = [("Bob", "main"), ("Orchestre", "main"), ("Bob", "composer")]

    got = cleaned(artists, ["Classique"])

    assert got == [("Orchestre", "main"), ("Bob", "composer")]


def test_each_main_dropped_says_why() -> None:
    artists = [
        ("Alice", "main"),
        ("Bob", "main"),
        ("Carol", "main"),
        ("Dan", "main"),
        ("Alice", "remixer"),
        ("Bob", "composer"),
        ("Carol", "conductor"),
    ]

    got = _mains_to_drop(artists, classical=True)

    assert got == [
        (("Alice", "main"), "also its guest or remixer"),
        (("Bob", "main"), "also its composer, on a classical release"),
        (("Carol", "main"), "also its conductor, on a classical release"),
    ]
    composer_who_conducts = [("Eve", "main"), ("Fay", "main"), ("Eve", "composer"), ("Eve", "conductor")]
    got = _mains_to_drop(composer_who_conducts, classical=True)

    assert got == [(("Eve", "main"), "also its composer, on a classical release")]


# The merge: a store's main no longer undoes the files' rule


def test_the_files_composer_stays_off_artist_when_a_store_states_him_main() -> None:
    """deemix files with a COMPOSER tag: the files' rule credits Puccini as composer; Deezer states him main."""
    files = release(("Berliner Philharmoniker", "main"), ("Giacomo Puccini", "composer"))
    files["genres"] = CLASSICAL
    store = release(("Giacomo Puccini", "main"), ("Berliner Philharmoniker", "main"))

    combined = clean_metadata(combine_metadatas(("Deezer", store), base=files))

    assert combined["tracks"]["1"]["1"]["artists"] == [
        ("Berliner Philharmoniker", "main"),
        ("Giacomo Puccini", "composer"),
    ]


def test_picard_files_naming_only_the_composer_lose_him_as_main_once_a_store_credits_him_composer(monkeypatch) -> None:
    """Picard's ARTIST is often the composer alone; Qobuz states him composer and the performers main."""
    stores_stating(monkeypatch, stating=("Qobuz",), assuming=())
    files = release(("Ludwig van Beethoven", "main"))
    files["genres"] = CLASSICAL
    qobuz = stating(
        release(
            ("Wiener Philharmoniker", "main"), ("Carlos Kleiber", "conductor"), ("Ludwig van Beethoven", "composer")
        )
    )

    combined = clean_metadata(combine_metadatas(("Qobuz", qobuz), base=files))

    artists = combined["tracks"]["1"]["1"]["artists"]
    assert ("Ludwig van Beethoven", "main") not in artists
    assert ("Ludwig van Beethoven", "composer") in artists
    assert ("Wiener Philharmoniker", "main") in artists


# What the rule promises, over generated classical tracks (AGENTS.md, #1154)

_NAMES = ["Alice", "Bob", "Carol", "Dan", "Eve"]
_ROLES = ["main", "main", "main", "guest", "remixer", "composer", "conductor", "producer"]


def _generated_tracks(count: int):
    rng = random.Random(1132)
    for _ in range(count):
        credits = [(rng.choice(_NAMES), rng.choice(_ROLES)) for _ in range(rng.randint(1, 6))]
        yield list(dict.fromkeys(credits)), rng.random() < 0.7


def test_the_cleaning_keeps_its_promises_on_generated_tracks() -> None:
    for artists, classical in _generated_tracks(3000):
        before = list(artists)
        after = cleaned(artists, CLASSICAL if classical else ["Pop"])
        again = cleaned(after, CLASSICAL if classical else ["Pop"])
        mains = [credit for credit in before if credit[1] == "main"]
        kept = [credit for credit in after if credit[1] == "main"]
        roles = {name for name, role in after if role in {"composer", "conductor"}}
        guests = {name for name, role in before if role in {"guest", "remixer"}}

        assert [credit for credit in before if credit[1] != "main"] == [c for c in after if c[1] != "main"], before
        assert kept == [credit for credit in mains if credit in kept], before
        assert kept or not mains, before
        # A main is dropped only for another of its credits on the track.
        assert all(credit in kept or credit[0] in guests | roles for credit in mains), before
        assert again == after, before
        if not classical:
            # Off classical, only the remixer and guest check drops a main.
            assert all(credit in kept or credit[0] in guests for credit in mains), before
        elif any(name not in roles for name, _ in kept):
            # A main also composer or conductor is kept only when every main is.
            assert not any(name in roles for name, _ in kept), before


# A scrape used alone (`salmon meta`, salmon web's lookup): the same rule, as descgen gives it from one URL (#1183)


def alone(store: str, artists: list[tuple[str, str]], genres: list[str]) -> dict:
    meta = release()
    meta["genres"] = genres
    meta["tracks"]["1"]["1"]["artists"] = list(artists)
    decide_alone(meta, store)
    return meta


def test_a_scrape_used_alone_credits_a_conductor_it_states_as_main_as_conductor_on_a_classical_release(
    monkeypatch,
) -> None:
    stores_stating(monkeypatch, stating=("Qobuz",), assuming=())
    meta = alone(
        "Qobuz",
        [("Fritz Reiner", "main"), ("Chicago Symphony Orchestra", "main"), ("Fritz Reiner", "conductor")],
        CLASSICAL,
    )

    want = [("Chicago Symphony Orchestra", "main"), ("Fritz Reiner", "conductor")]
    assert meta["tracks"]["1"]["1"]["artists"] == want
    assert meta["artists"] == want


def test_a_scrape_used_alone_drops_the_main_of_a_track_they_remix(monkeypatch) -> None:
    """Deezer's mixing DJ on Club Future Nostalgia, main on the tracks she remixes beside the singer."""
    stores_stating(monkeypatch, stating=("Deezer",), assuming=())
    meta = alone(
        "Deezer",
        [("Dua Lipa", "main"), ("The Blessed Madonna", "main"), ("The Blessed Madonna", "remixer")],
        ["Pop"],
    )

    want = [("Dua Lipa", "main"), ("The Blessed Madonna", "remixer")]
    assert meta["tracks"]["1"]["1"]["artists"] == want
    assert meta["artists"] == want


def test_a_scrape_used_alone_runs_the_whole_cleaning() -> None:
    """All of `clean_metadata` runs, not only its credit rule. A real scrape has cleared this catno already
    (`scrape_release`), so no user sees this step change."""
    meta = release()
    meta["catno"], meta["upc"] = "602 547 00", "60254700"

    decide_alone(meta, "Deezer")

    assert meta["catno"] is None


def test_as_before_a_scrape_used_alone_keeps_a_composer_main_off_classical() -> None:
    artists = [("Alice", "main"), ("Bob", "main"), ("Bob", "composer")]

    meta = alone("Deezer", artists, ["Pop"])

    assert meta["tracks"]["1"]["1"]["artists"] == artists


def test_as_before_a_scrape_used_alone_keeps_the_only_main_of_a_track_they_remix_or_conduct() -> None:
    remixing = [("Alice", "main"), ("Alice", "remixer")]
    conducting = [("Fritz Reiner", "main"), ("Fritz Reiner", "conductor"), ("Johann Strauss II", "composer")]

    assert alone("Deezer", remixing, ["Pop"])["tracks"]["1"]["1"]["artists"] == remixing
    assert alone("Qobuz", conducting, CLASSICAL)["tracks"]["1"]["1"]["artists"] == conducting


def test_a_scrape_used_alone_credits_generated_tracks_as_descgen_does_from_its_url(monkeypatch) -> None:
    """descgen with one URL merges that one scrape and cleans it: the same credits, track by track and the release's."""
    stores_stating(monkeypatch, stating=("Qobuz",), assuming=("Deezer",))
    for at, (artists, classical) in enumerate(_generated_tracks(600)):
        store = ("Qobuz", "Deezer")[at % 2]
        genres = CLASSICAL if classical else ["Pop"]
        meta = release()
        meta["genres"] = genres
        meta["tracks"]["1"]["1"]["artists"] = list(artists)
        if store == "Qobuz":
            stating(meta)
        merged = clean_metadata(combine_metadatas((store, deepcopy(meta))))
        merged["artists"], merged["tracks"] = generate_artists(merged["tracks"])

        decide_alone(meta, store)

        assert meta["tracks"]["1"]["1"]["artists"] == merged["tracks"]["1"]["1"]["artists"], (store, artists, genres)
        assert meta["artists"] == merged["artists"], (store, artists, genres)
