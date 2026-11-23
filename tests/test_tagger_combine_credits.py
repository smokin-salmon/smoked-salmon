"""The merge's credits decided from each source's claims (ADR 0007): who each claim comes from, the spellings, and
the disputes salmon prints."""

import contextlib
import io
from copy import deepcopy

import anyio
import msgspec
import pytest
from helpers.deezer import deezer_song, deezer_tracks
from helpers.tagger_combine import make_tracks, one_track, stating, stores_stating
from test_common_credits import files_claim, store_claim

from salmon import cfg
from salmon.common.credits import Claim
from salmon.tagger.combine import _decide_credits, _join_split_names, combine_metadatas, combine_tracks, decide_alone
from salmon.tagger.metadata import clean_metadata
from salmon.tagger.retagger import create_remixer_list
from salmon.tagger.sources.base import assign_track_totals, construct_replacement_list, generate_artists
from salmon.tagger.sources.deezer import Scraper as Deezer


def release(*artists: tuple[str, str], label: str | None = None) -> dict:
    return {
        "artists": list(artists),
        "title": "Album",
        "group_year": "2026",
        "year": "2026",
        "date": None,
        "edition_title": None,
        "label": label,
        "catno": None,
        "rls_type": None,
        "genres": [],
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
        "source": "WEB",
        "cover": None,
        "upc": None,
        "comment": None,
        "urls": [],
        "tracks": one_track(list(artists), "Song"),
    }


def two_tracks(first: list[tuple[str, str]], second: list[tuple[str, str]]) -> dict:
    meta = release()
    meta["tracks"] = make_tracks({"1": ["1", "2"]}, isrc_prefix=None)
    meta["tracks"]["1"]["1"]["artists"], meta["tracks"]["1"]["2"]["artists"] = first, second
    return meta


def test_the_files_main_is_kept_where_a_store_says_guest_and_salmon_says_so(capsys: pytest.CaptureFixture) -> None:
    combined = combine_metadatas(
        ("Qobuz", release(("Alice", "main"), ("Bob", "guest"))), base=release(("Alice", "main"), ("Bob", "main"))
    )

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "main")]
    out = capsys.readouterr().out
    assert "Kept the files' main where stores say guest:\n  Bob (guest at Qobuz)\n" in out


def test_the_files_guest_is_kept_where_a_store_says_main(capsys: pytest.CaptureFixture) -> None:
    combine_metadatas(
        ("Deezer", release(("Alice", "main"), ("Bob", "main"))), base=release(("Alice", "main"), ("Bob", "guest"))
    )

    out = capsys.readouterr().out
    assert "Kept the files' guest where stores say main:\n  Bob (main at Deezer)\n" in out


def test_with_no_files_the_first_store_is_the_base(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    stores_stating(monkeypatch, stating=(), assuming=("MusicBrainz", "Deezer"))
    combined = combine_metadatas(
        ("MusicBrainz", release(("Alice", "main"), ("Bob", "guest"))),
        ("Deezer", release(("Alice", "main"), ("Bob", "main"))),
    )

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "main")]
    out = capsys.readouterr().out
    assert "Kept Deezer's main where other stores say guest:\n  Bob (guest at MusicBrainz)\n" in out


def test_names_the_same_stores_dispute_share_a_line_whatever_the_order(capsys: pytest.CaptureFixture) -> None:
    files = two_tracks([("Bob", "main"), ("Carol", "main")], [("Carol", "main")])
    deezer = two_tracks([("Bob", "guest")], [("Carol", "guest")])
    qobuz = two_tracks([("Bob", "guest"), ("Carol", "guest")], [])

    combine_metadatas(("Qobuz", qobuz), ("Deezer", deezer), base=deepcopy(files))

    out = capsys.readouterr().out
    assert "\n  Bob, Carol (guest at Deezer, Qobuz)\n" in out


def test_a_store_credit_the_merge_matches_to_the_base_is_one_name_to_dispute(capsys: pytest.CaptureFixture) -> None:
    """Qobuz's "JAY Z" is the merge's match for Deezer's "Jay-Z" (`comparable`), so it disputes the files' main too."""
    combine_metadatas(
        ("Qobuz", release(("JAY Z", "guest"))), ("Deezer", release(("Jay-Z", "guest"))), base=release(("Jay-Z", "main"))
    )

    out = capsys.readouterr().out
    assert "\n  Jay-Z (guest at Deezer, Qobuz)\n" in out


def test_a_name_spelled_two_ways_is_one_name_to_dispute(capsys: pytest.CaptureFixture) -> None:
    combine_metadatas(("Deezer", release(("Kool & the Gang", "guest"))), base=release(("Kool & The Gang", "main")))

    out = capsys.readouterr().out
    assert "\n  Kool & the Gang (guest at Deezer)\n" in out


def test_as_before_nothing_is_printed_when_the_sources_agree(capsys: pytest.CaptureFixture) -> None:
    combine_metadatas(("Qobuz", release(("Alice", "main"))), base=release(("Alice", "main")))

    out = capsys.readouterr().out
    assert "Kept" not in out


def test_as_before_a_name_takes_the_spelling_with_fewer_capitals() -> None:
    files = release(("Kool & The Gang", "main"))

    combined = combine_metadatas(("Deezer", release(("Kool & the Gang", "guest"))), base=files)

    assert combined["tracks"]["1"]["1"]["artists"] == [("Kool & the Gang", "main")]


def test_as_before_a_fragment_the_merge_drops_stays_dropped() -> None:
    combined = combine_metadatas(("Deezer", release(("Ava", "main"))), base=release(("Ava Daniel", "main")))

    assert combined["tracks"]["1"]["1"]["artists"] == [("Ava Daniel", "main")]


def test_a_badly_split_name_is_joined_back_before_the_decision_weighs_it() -> None:
    # #1109: deciding first took Max's main away, as Max is also a guest, and the repair then had no Max to join Ava to.
    files = two_tracks([("Max", "guest"), ("Ava", "main"), ("Max", "main")], [("Ava Max", "composer")])

    combined = combine_metadatas(("Deezer", two_tracks([], [])), base=files)

    assert combined["tracks"]["1"]["1"]["artists"] == [("Max", "guest"), ("Ava Max", "main")]


def decided(claims: list[Claim], *others: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Track 1's credits as the merge decides them from these claims, the release's other tracks credited `others`
    (stated, by the files)."""
    tracks = make_tracks({"1": [str(num) for num in range(1, len(others) + 2)]}, isrc_prefix=None)
    first = tracks["1"]["1"]
    first["artists"] = list(dict.fromkeys((claim.name, claim.role) for claim in claims))
    for num, artists in enumerate(others, 2):
        tracks["1"][str(num)]["artists"] = artists
    _decide_credits(tracks, {id(first): claims}, "files")
    return first["artists"]


@pytest.mark.parametrize("store", ["Deezer", "Qobuz"], ids=["a store's guess", "a store's stated main"])
def test_a_claim_the_decision_overrules_completes_no_join(monkeypatch: pytest.MonkeyPatch, store: str) -> None:
    # statelycurmudgeon on #1164: the files' stated guest beats the store's main for Max, so that main is not there to
    # join Ava to, as before the repair ran first.
    stores_stating(monkeypatch, ("Qobuz",), ("Deezer",))
    files = two_tracks([("Ava", "main"), ("Max", "guest")], [("Ava & Max", "main")])
    theirs = two_tracks([("Ava", "main"), ("Max", "main")], [("Ava & Max", "main")])

    combined = combine_metadatas((store, theirs), base=files)

    assert combined["tracks"]["1"]["1"]["artists"] == [("Ava", "main"), ("Max", "guest")]
    assert combined["artists"] == [("Ava", "main"), ("Max", "guest"), ("Ava & Max", "main")]


def test_a_scrape_used_alone_joins_no_part_its_first_guess_overrules(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deezer guesses Max as a guest, then as a main: the first guess wins, so Max's main is not there to join Ava to.
    stores_stating(monkeypatch, (), ("Deezer",))
    alone = two_tracks([("Max", "guest"), ("Ava", "main"), ("Max", "main")], [("Ava Max", "composer")])

    decide_alone(alone, "Deezer")

    assert alone["tracks"]["1"]["1"]["artists"] == [("Max", "guest"), ("Ava", "main")]


def test_a_scrape_used_alone_has_its_titles_written_before_a_split_name_is_joined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review on #1164: the featuring clause names Max as the store credits him, before Ava and Max are joined back.
    monkeypatch.setattr(cfg.upload.formatting, "strip_useless_versions", True)
    alone = two_tracks([("Bob", "main"), ("Ava", "guest"), ("Max", "guest")], [("Ava Max", "composer")])
    alone["tracks"]["1"]["1"]["title"] = "Song (feat. Max)"

    decide_alone(alone, "Deezer")

    assert alone["tracks"]["1"]["1"]["title"] == "Song"
    assert alone["tracks"]["1"]["1"]["artists"] == [("Bob", "main"), ("Ava Max", "guest")]


@pytest.mark.parametrize("store", ["Deezer", "Qobuz"], ids=["a store's guess", "a store's stated guest"])
def test_a_claim_that_agrees_with_a_kept_claim_follows_it_and_disputes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, store: str
) -> None:
    # Review on #1164: the store's guest loses to the files' main for Max, but agrees with the files' own guest credit
    # of Max, which stays: it is a claim of that credit, not of the whole name the main is joined into.
    stores_stating(monkeypatch, ("Qobuz",), ("Deezer",))
    files = two_tracks([("Ava", "main"), ("Max", "main"), ("Max", "guest")], [("Ava Max", "composer")])

    combined = combine_metadatas((store, two_tracks([("Max", "guest")], [("Ava Max", "composer")])), base=files)

    assert combined["tracks"]["1"]["1"]["artists"] == [("Ava Max", "main"), ("Max", "guest")]
    out = capsys.readouterr().out
    assert "Kept" not in out


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        (
            [files_claim("Ava"), files_claim("Max"), store_claim("Deezer", "Ava", "main")],
            [("Ava Max", "main")],
        ),
        (
            [
                files_claim("Ava"),
                files_claim("Max"),
                store_claim("Deezer", "Ava", "main"),
                store_claim("Deezer", "Max", "guest"),
            ],
            [("Ava Max", "main")],
        ),
        (
            [
                files_claim("Ava", "guest"),
                files_claim("Max"),
                store_claim("Deezer", "Ava", "main"),
                store_claim("Deezer", "Max", "main"),
            ],
            [("Ava", "guest"), ("Max", "main")],
        ),
        (
            [files_claim("Ava"), files_claim("Max", "guest"), store_claim("Deezer", "Max", "main")],
            [("Ava", "main"), ("Max", "guest")],
        ),
    ],
    ids=[
        "one source's part is joined too",
        "an overruled part follows the whole name and loses again",
        "a guess against a stated role completes no join",
        "a guess beside a stated part in another role completes no join",
    ],
)
def test_a_name_is_joined_back_from_the_claims_the_decision_keeps(
    claims: list[Claim], expected: list[tuple[str, str]]
) -> None:
    """The parts are joined over every source's claims on the track, role by role, as the repair joins a track's
    credits, but for a claim the decision overrules: it joins nothing, and follows the claim that beat it. The credits
    are then those of deciding first and joining after, but for one source's part in two roles (#1109)."""
    assert decided(claims, [("Ava Max", "composer")]) == expected


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ([files_claim("Ava"), files_claim("Max"), files_claim("Ray")], [("Ava Max Ray", "main")]),
        (
            [
                files_claim("Ava", "guest"),
                files_claim("Max", "guest"),
                files_claim("Ava"),
                files_claim("Max"),
                files_claim("Ray"),
            ],
            [("Ava Max", "guest"), ("Ava Max Ray", "main")],
        ),
    ],
    ids=["in one role", "the shorter name in another role stays"],
)
def test_a_name_split_in_three_is_joined_back_whole(claims: list[Claim], expected: list[tuple[str, str]]) -> None:
    """ "Ava Max" is joined, then "Ava Max" and "Ray": each part's claim is a claim of the longest name in its role."""
    assert decided(claims, [("Ava Max", "composer")], [("Ava Max Ray", "composer")]) == expected


def test_each_claim_of_a_joined_part_keeps_its_source_and_flag() -> None:
    """Deezer's guessed guests lose to Qobuz's mains, so they follow them into the whole name, as guests."""
    claims = [
        store_claim("Qobuz", "Ava", "main", assumed=False),
        store_claim("Qobuz", "Max", "main"),
        store_claim("Deezer", "Ava", "guest"),
        store_claim("Deezer", "Max", "guest"),
    ]

    to_replace = construct_replacement_list([("Ava", "main"), ("Max", "main"), ("Ava Max", "composer")])

    joined = _join_split_names(claims, to_replace)

    assert [(claim.source, claim.name, claim.role, claim.assumed) for claim in joined] == [
        ("Qobuz", "Ava Max", "main", False),
        ("Qobuz", "Ava Max", "main", True),
        ("Deezer", "Ava Max", "guest", True),
        ("Deezer", "Ava Max", "guest", True),
    ]


def test_claims_the_decision_overrules_are_not_credited_beside_the_name_that_beat_them() -> None:
    """Deezer's guesses of Ava and Max as guests come first, so Qobuz's guessed mains lose: they follow Deezer's
    claims into the whole name, and are not left as mains of their own."""
    claims = [
        store_claim("Deezer", "Ava", "guest"),
        store_claim("Deezer", "Max", "guest"),
        store_claim("Qobuz", "Ava", "main"),
        store_claim("Qobuz", "Max", "main"),
    ]

    assert decided(claims, [("Ava Max", "composer")]) == [("Ava Max", "guest")]


def test_a_split_name_is_joined_back_under_the_spelling_its_artist_takes() -> None:
    # #1109 and #1131 together: "Ava" and "Max" join into the one artist "The Ava Max" / "Ava Max" is, spelled once.
    files, deezer = release(), release()
    files["tracks"] = make_tracks({"1": ["1", "2", "3"]}, isrc_prefix=None)
    deezer["tracks"] = make_tracks({"1": ["1", "2", "3"]}, isrc_prefix=None)
    files["tracks"]["1"]["1"]["artists"] = [("Ava", "main"), ("Max", "main")]
    files["tracks"]["1"]["2"]["artists"] = [("The Ava Max", "composer")]
    files["tracks"]["1"]["3"]["artists"] = [("Ava Max", "composer")]
    for track in deezer["tracks"]["1"].values():
        track["artists"] = []

    combined = combine_metadatas(("Deezer", deezer), base=files)

    assert [combined["tracks"]["1"][num]["artists"] for num in ("1", "2", "3")] == [
        [("The Ava Max", "main")],
        [("The Ava Max", "composer")],
        [("The Ava Max", "composer")],
    ]


def test_as_before_a_featuring_clause_naming_a_dropped_fragment_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    # The store's "Simon" is a fragment of "Simon & Garfunkel" and goes, so nothing credits the clause's Simon.
    monkeypatch.setattr(cfg.upload.formatting, "strip_useless_versions", True)
    files = release(("Simon & Garfunkel", "main"))
    files["tracks"]["1"]["1"]["title"] = "Song (feat. Simon)"

    combined = combine_metadatas(("Deezer", release(("Simon & Garfunkel", "main"), ("Simon", "guest"))), base=files)

    assert combined["tracks"]["1"]["1"]["title"] == "Song (feat. Simon)"


@pytest.mark.parametrize(
    ("written", "expected"),
    [("Herbaliser", "Herbaliser X"), ("The Herbaliser", "Herbaliser")],
    ids=["the files' spelling, which salmon writes", "a store's spelling, which it doesn't"],
)
def test_a_corrected_artist_is_one_credit_under_every_spelling(
    monkeypatch: pytest.MonkeyPatch, written: str, expected: str
) -> None:
    # A correction names the spelling salmon writes, and the artist's other spellings follow it: one credit either way.
    stores_stating(monkeypatch, ("Qobuz",), ())
    monkeypatch.setattr(cfg.metadata.corrections, "artists", {written: "Herbaliser X"})

    combined = combine_metadatas(("Qobuz", release(("The Herbaliser", "main"))), base=release(("Herbaliser", "main")))

    assert credits(combined) == [[(expected, "main")]]


def test_a_correction_is_not_shown_as_a_join(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    stores_stating(monkeypatch, ("Qobuz",), ())
    monkeypatch.setattr(cfg.metadata.corrections, "artists", {"Herbaliser": "Herbaliser X"})

    combine_metadatas(("Qobuz", release(("The Herbaliser", "main"))), base=release(("Herbaliser", "main")))

    out = capsys.readouterr().out
    assert "Herbaliser: The Herbaliser (Qobuz)" in out
    assert "Herbaliser X:" not in out


def test_a_remixer_discogs_credits_under_another_name_takes_its_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    # Discogs' credited name is a REMIXER name of its own, not a credit on the track: the correction still reaches it.
    monkeypatch.setattr(cfg.metadata.corrections, "artists", {"Herbalizer": "Herbaliser X"})
    discogs = credited_as(
        release(("Bob", "main"), ("The Herbaliser", "remixer")), "The Herbaliser", "remixer", "Herbalizer"
    )
    discogs["tracks"]["1"]["1"]["credited_remixers"] = ["The Herbaliser", "Herbalizer"]  # as the parser lists them

    combined = combine_metadatas(("Discogs", discogs), base=release(("Bob", "main")))

    assert combined["tracks"]["1"]["1"]["credited_remixers"] == ["The Herbaliser", "Herbaliser X"]


def test_a_corrected_artist_keeps_the_name_discogs_credits_it_as(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.metadata.corrections, "artists", {"Roger Taylor": "Roger Meddows Taylor"})
    discogs = credited_as(
        release(("Queen", "main"), ("Roger Taylor", "producer")), "Roger Taylor", "producer", "Taylor"
    )

    combined = combine_metadatas(("Discogs", discogs), base=release(("Queen", "main"), ("Taylor", "guest")))

    assert credits(combined) == [[("Queen", "main"), ("Taylor", "guest"), ("Taylor", "producer")]]


def test_a_featuring_clause_naming_discogs_name_for_a_credited_name_goes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Discogs credits "The Groove Unit Collective" as the files' "The Groove Unit": the clause names the guest.
    monkeypatch.setattr(cfg.upload.formatting, "strip_useless_versions", True)
    files = release(("Bob", "main"), ("Groove Unit", "guest"))
    files["tracks"]["1"]["1"]["title"] = "Song (feat. The Groove Unit Collective)"
    discogs = credited_as(
        release(("Bob", "main"), ("The Groove Unit Collective", "guest")),
        "The Groove Unit Collective",
        "guest",
        "The Groove Unit",
    )

    combined = combine_metadatas(("Discogs", discogs), base=files)

    assert combined["tracks"]["1"]["1"]["title"] == "Song"


def test_remixers_split_and_joined_back_keep_their_remixer_tag() -> None:
    # "Simon" and "Garfunkel" join into "Simon and Garfunkel" as remixer: the credited remixer follows, for REMIXER.
    files = two_tracks(
        [("Bob", "main"), ("Simon", "remixer"), ("Garfunkel", "remixer")], [("Simon and Garfunkel", "composer")]
    )
    files["tracks"]["1"]["1"]["credited_remixers"] = ["Simon", "Garfunkel"]

    track = combine_metadatas(("Deezer", two_tracks([], [])), base=files)["tracks"]["1"]["1"]

    assert track["artists"] == [("Bob", "main"), ("Simon and Garfunkel", "remixer")]
    assert create_remixer_list(track) == ["Simon and Garfunkel"]


def test_as_before_a_featuring_clause_is_not_admitted_by_a_producer_of_the_same_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The guest "Herbaliser" is a fragment of the main and goes; the producer "The Herbaliser" performs nothing.
    monkeypatch.setattr(cfg.upload.formatting, "strip_useless_versions", True)
    files = release(("Herbaliser Collective", "main"), ("Herbaliser", "guest"), ("The Herbaliser", "producer"))
    files["tracks"]["1"]["1"]["title"] = "Song (feat. Herbaliser)"

    combined = combine_metadatas(("Deezer", release()), base=files)

    assert combined["tracks"]["1"]["1"]["title"] == "Song (feat. Herbaliser)"


def test_one_title_naming_a_remixer_in_two_spellings_gives_one_remixer() -> None:
    files, deezer = release(("Bob", "main")), release(("Bob", "main"))
    for meta in (files, deezer):
        meta["tracks"]["1"]["1"]["title"] = "Song (Jay-Z & Jay Z Remix)"

    combined = combine_metadatas(("Deezer", deezer), base=files)

    assert combined["tracks"]["1"]["1"]["artists"] == [("Bob", "main"), ("Jay-Z", "remixer")]


def test_as_before_a_store_alone_keeps_its_credits(capsys: pytest.CaptureFixture) -> None:
    combined = combine_metadatas(("Deezer", release(("Alice", "main"), ("Bob", "guest"))))

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "guest")]
    out = capsys.readouterr().out
    assert "Kept" not in out


def test_a_label_named_after_a_guest_on_every_track_is_not_self_released() -> None:
    """The release's artists come from the decided credits: Alice, a guest on every track, is not one of its mains,
    even though Deezer calls her main."""
    files = release(("Bob", "main"), ("Alice", "guest"), label="Alice Records")

    combined = combine_metadatas(("Deezer", release(("Bob", "main"), ("Alice", "main"))), base=files)

    assert combined["label"] == "Alice Records"


def test_each_claim_comes_from_its_source_with_the_name_as_printed() -> None:
    base = one_track([("Alice", "main"), ("K-Ci and JoJo", "main")], "Song (Bob Remix)")
    meta = one_track([("Alice", "main"), ("K-Ci & JoJo", "main")], "Song (Bob Remix)")
    meta["1"]["1"]["artist_variations"] = [{"name": "K-Ci & JoJo", "role": "main", "anv": "K-Ci and JoJo"}]
    claims = {}

    track = combine_tracks(base, meta, True, claims, ("files", "Discogs"))["1"]["1"]

    assert [(c.source, c.name, c.credited_as, c.role, c.assumed) for c in claims[id(track)]] == [
        ("files", "Alice", "Alice", "main", False),
        ("files", "K-Ci and JoJo", "K-Ci and JoJo", "main", False),
        ("Discogs", "Alice", "Alice", "main", True),
        ("Discogs", "K-Ci & JoJo", "K-Ci and JoJo", "main", True),
        ("Discogs", "Bob", "Bob", "remixer", True),
    ]


def test_the_decision_is_written_to_the_track() -> None:
    """Until a store states its roles (step 3), first-wins and the decision agree; here a stated claim comes later."""
    tracks = one_track([("Alice", "main"), ("Alice", "guest")], "Song")
    track = tracks["1"]["1"]
    claims = {
        id(track): [
            Claim("Tidal", "Alice", "Alice", "main", assumed=True),
            Claim("Qobuz", "Alice", "Alice", "guest", assumed=False),
        ]
    }

    _decide_credits(tracks, claims, "Tidal")

    assert track["artists"] == [("Alice", "guest")]


def test_with_no_files_a_stated_main_beats_another_store_s_assumed_guest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    stores_stating(monkeypatch, stating=("Qobuz",), assuming=("Deezer",))
    combined = combine_metadatas(
        ("Deezer", release(("Alice", "main"), ("Bob", "guest"))),
        ("Qobuz", stating(release(("Alice", "main"), ("Bob", "main")))),
    )

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "main")]
    out = capsys.readouterr().out
    assert "Kept Qobuz's main where other stores say guest:\n  Bob (guest at Deezer)\n" in out


def test_a_stated_credit_takes_the_name_the_merge_matched_it_to(monkeypatch: pytest.MonkeyPatch) -> None:
    stores_stating(monkeypatch, stating=("Qobuz",), assuming=("Deezer",))
    combined = combine_metadatas(
        ("Deezer", release(("Kool & the Gang", "guest"))), ("Qobuz", stating(release(("Kool & The Gang", "main"))))
    )

    assert combined["tracks"]["1"]["1"]["artists"] == [("Kool & the Gang", "main")]


def test_as_before_a_credit_a_stating_store_guessed_beats_no_one(monkeypatch: pytest.MonkeyPatch) -> None:
    stores_stating(monkeypatch, stating=("Qobuz",), assuming=("Deezer",))
    combined = combine_metadatas(
        ("Deezer", release(("Alice", "main"), ("Bob", "guest"))),
        ("Qobuz", stating(release(("Alice", "main"), ("Bob", "main")), ("Bob", "main"))),
    )

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "guest")]


def test_as_before_a_guess_is_found_under_the_store_s_own_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Qobuz's "Kool & The Gang" is matched to Deezer's "Kool & the Gang" main, and is still the guess it marked."""
    stores_stating(monkeypatch, stating=("Qobuz",), assuming=("Tidal", "Deezer"))
    combined = combine_metadatas(
        ("Tidal", release(("Kool & the Gang", "guest"))),
        ("Deezer", release(("Kool & the Gang", "main"))),
        ("Qobuz", stating(release(("Kool & The Gang", "main")), ("Kool & The Gang", "main"))),
    )

    assert combined["tracks"]["1"]["1"]["artists"] == [("Kool & the Gang", "guest")]


def test_a_stating_store_as_the_base_has_its_own_guesses_weighed(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no files the first store is the base: Qobuz's guessed main loses to the guest Bandcamp states."""
    stores_stating(monkeypatch, stating=("Qobuz", "Bandcamp"), assuming=())
    combined = combine_metadatas(
        ("Qobuz", stating(release(("Alice", "main"), ("Bob", "main")), ("Bob", "main"))),
        ("Bandcamp", stating(release(("Alice", "main"), ("Bob", "guest")))),
    )

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "guest")]


def test_a_store_alone_has_its_guesses_weighed_before_they_are_dropped() -> None:
    combined = combine_metadatas(("Qobuz", stating(release(("Bob", "guest"), ("Bob", "main")), ("Bob", "guest"))))

    track = combined["tracks"]["1"]["1"]
    assert track["artists"] == [("Bob", "main")]
    assert "assumed_artists" not in track


def test_as_before_a_store_giving_one_name_two_roles_disputes_nothing(capsys: pytest.CaptureFixture) -> None:
    qobuz = stating(release(("Alice", "main"), ("Bob", "main"), ("Bob", "guest")), ("Bob", "guest"))

    combined = combine_metadatas(("Qobuz", qobuz), base=release(("Alice", "main")))

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "main")]
    out = capsys.readouterr().out
    assert "Kept" not in out


def test_a_stating_store_s_claims_are_stated_but_for_its_guesses_and_a_title_s_remixers() -> None:
    base = one_track([("Alice", "main")], "Song (Bob Remix)")
    meta = one_track([("Alice", "main"), ("Carol", "guest")], "Song (Bob Remix)")
    meta["1"]["1"]["assumed_artists"] = [("Carol", "guest")]
    claims = {}

    track = combine_tracks(base, meta, True, claims, ("files", "Qobuz"))["1"]["1"]

    assert [(c.source, c.name, c.role, c.assumed) for c in claims[id(track)]] == [
        ("files", "Alice", "main", False),
        ("Qobuz", "Alice", "main", False),
        ("Qobuz", "Carol", "guest", True),
        ("Qobuz", "Bob", "remixer", True),
    ]


def remixed(store: str, files: bool = False) -> dict:
    """Two tracks, each a remix by its title; the store credits the first's remixer (not the one its title names),
    the files none."""
    meta = two_tracks(
        [("Alice", "main")] if files else [("Alice", "main"), ("Remi Xer", "remixer")], [("Alice", "main")]
    )
    meta["tracks"]["1"]["1"]["title"], meta["tracks"]["1"]["2"]["title"] = (
        "One (Attitude Remix)",
        "Two (Other Xer Remix)",
    )
    return meta


def test_a_stating_store_s_remixer_on_one_track_leaves_another_s_title_read(monkeypatch: pytest.MonkeyPatch) -> None:
    stores_stating(monkeypatch, stating=("Qobuz",), assuming=())
    tracks = combine_metadatas(("Qobuz", remixed("Qobuz")), base=remixed("files", files=True))["tracks"]["1"]

    assert tracks["1"]["artists"] == [("Alice", "main"), ("Remi Xer", "remixer")]
    assert tracks["2"]["artists"] == [("Alice", "main"), ("Other Xer", "remixer")]


def test_as_before_an_assuming_store_s_remixer_on_one_track_reads_no_title(monkeypatch: pytest.MonkeyPatch) -> None:
    stores_stating(monkeypatch, stating=(), assuming=("Deezer",))
    tracks = combine_metadatas(("Deezer", remixed("Deezer")), base=remixed("files", files=True))["tracks"]["1"]

    assert tracks["1"]["artists"] == [("Alice", "main"), ("Remi Xer", "remixer")]
    assert tracks["2"]["artists"] == [("Alice", "main")]


# One artist in two spellings is one credit (#1131): the name key, and Discogs' credited names (ANVs) on a track.

DISCOGS_FIRST = "https://www.discogs.com/release/1"


def credited_as(meta: dict, name: str, role: str, anv: str) -> dict:
    """The release as Discogs hands it to the merge: `name` credited on its first track as `anv`."""
    meta["tracks"]["1"]["1"].setdefault("artist_variations", []).append({"name": name, "role": role, "anv": anv})
    return meta


def credits(meta: dict) -> list[list[tuple[str, str]]]:
    return [track["artists"] for disc in meta["tracks"].values() for track in disc.values()]


@pytest.mark.parametrize("source_url", [None, DISCOGS_FIRST], ids=["by preference", "Discogs chosen"])
def test_a_name_discogs_credits_under_another_is_one_remixer_whichever_store_comes_first(
    source_url: str | None,
) -> None:
    """MusicBrainz's 'Rick “Hum” Vee' is Discogs' 'Rick "Hum" Vee', whom Discogs credits as the files' "Rick Vee"."""
    combined = combine_metadatas(
        ("MusicBrainz", release(("Alice", "main"), ("Rick “Hum” Vee", "remixer"))),
        (
            "Discogs",
            credited_as(
                release(("Alice", "main"), ('Rick "Hum" Vee', "remixer")), 'Rick "Hum" Vee', "remixer", "Rick Vee"
            ),
        ),
        base=release(("Alice", "main"), ("Rick Vee", "remixer")),
        source_url=source_url,
    )

    assert credits(combined) == [[("Alice", "main"), ("Rick Vee", "remixer")]]


@pytest.mark.parametrize("source_url", [None, DISCOGS_FIRST], ids=["by preference", "Discogs chosen"])
def test_a_name_discogs_credits_as_the_files_spelling_joins_another_store_s_canonical_name(
    source_url: str | None,
) -> None:
    """Discogs credits "Bob Smith" as the files' "Bobby Smith", so MusicBrainz's "Bob Smith" is the same artist."""
    combined = combine_metadatas(
        ("MusicBrainz", release(("Bob Smith", "main"))),
        ("Discogs", credited_as(release(("Bob Smith", "main")), "Bob Smith", "main", "Bobby Smith")),
        base=release(("Bobby Smith", "main")),
        source_url=source_url,
    )

    assert credits(combined) == [[("Bobby Smith", "main")]]


def test_with_discogs_as_the_base_its_own_credited_names_join_another_store_s() -> None:
    combined = combine_metadatas(
        ("Discogs", credited_as(release(("Bob Smith", "main")), "Bob Smith", "main", "Bobby Smith")),
        ("Deezer", release(("Bobby Smith", "main"))),
        source_url=DISCOGS_FIRST,
    )

    assert credits(combined) == [[("Bob Smith", "main")]]


def test_as_before_a_name_discogs_credits_on_one_track_joins_nothing_on_another() -> None:
    """Discogs credits Roger Taylor as "Taylor" on the first track only; the second track's Taylor is someone else."""
    tracks = two_tracks(
        [("Queen", "main"), ("Roger Taylor", "remixer")],
        [("Queen", "main"), ("Taylor", "guest"), ("Roger Taylor", "remixer")],
    )["tracks"]
    tracks["1"]["1"]["artist_variations"] = [{"name": "Roger Taylor", "role": "remixer", "anv": "Taylor"}]

    _decide_credits(tracks, {}, "Discogs")

    assert tracks["1"]["2"]["artists"] == [("Queen", "main"), ("Taylor", "guest"), ("Roger Taylor", "remixer")]


def test_a_store_name_discogs_credits_with_the_article_keeps_the_files_spelling() -> None:
    """Discogs credits "The Groove Unit Collective" as "The Groove Unit", the files' "Groove Unit": the merge matches
    it to the files' credit, so its longer name never reaches the track to make the files' a fragment of it."""
    discogs = credited_as(
        release(("The Groove Unit Collective", "main")), "The Groove Unit Collective", "main", "The Groove Unit"
    )

    combined = combine_metadatas(("Discogs", discogs), base=release(("Groove Unit", "main")))

    assert credits(combined) == [[("Groove Unit", "main")]]


def test_a_remixer_a_title_names_with_the_article_keeps_the_files_spelling() -> None:
    deezer = release(("Alice", "main"))
    deezer["tracks"]["1"]["1"]["title"] = "Song (The Marlowes Remix)"

    combined = combine_metadatas(("Deezer", deezer), base=release(("Alice", "main"), ("Marlowes", "remixer")))

    assert credits(combined) == [[("Alice", "main"), ("Marlowes", "remixer")]]


@pytest.mark.parametrize("source_url", [None, DISCOGS_FIRST], ids=["by preference", "Discogs chosen"])
def test_a_stated_main_spelled_without_the_article_is_kept_beside_a_store_s_guessed_guest(
    monkeypatch: pytest.MonkeyPatch, source_url: str | None
) -> None:
    """DJ-Kicks: the files' "Herbaliser" is Discogs' "The Herbaliser", credited as Qobuz's "Herbalizer"."""
    stores_stating(monkeypatch, stating=("Qobuz",), assuming=("MusicBrainz", "Discogs"))
    combined = combine_metadatas(
        ("Qobuz", stating(release(("Herbalizer", "guest")), ("Herbalizer", "guest"))),
        ("MusicBrainz", release(("The Herbaliser", "main"))),
        ("Discogs", credited_as(release(("The Herbaliser", "main")), "The Herbaliser", "main", "Herbalizer")),
        base=release(("Herbaliser", "main")),
        source_url=source_url,
    )

    assert credits(combined) == [[("Herbaliser", "main")]]


def test_a_group_spelled_with_and_without_the_article_is_one_artist_across_tracks() -> None:
    files = two_tracks([("Ahmad Jamal Trio", "main")], [("Ahmad Jamal Trio", "main")])
    musicbrainz = two_tracks([("Ahmad Jamal Trio", "main")], [("The Ahmad Jamal Trio", "main")])

    combined = combine_metadatas(("MusicBrainz", musicbrainz), base=files)

    assert credits(combined) == [[("Ahmad Jamal Trio", "main")], [("Ahmad Jamal Trio", "main")]]
    assert combined["artists"] == [("Ahmad Jamal Trio", "main")]


def test_a_group_written_with_and_or_an_ampersand_is_one_main() -> None:
    combined = combine_metadatas(
        ("Deezer", release(("Dick Dale and His Del-Tones", "main"))),
        base=release(("Dick Dale & His Del-Tones", "main")),
    )

    assert credits(combined) == [[("Dick Dale & His Del-Tones", "main")]]


def test_one_artist_in_two_spellings_and_two_roles_is_one_credit_the_files_state(
    capsys: pytest.CaptureFixture,
) -> None:
    """MBDTF: the files' main "JAŸ-Z" is Deezer's guest "JAY Z", one by `comparable`, so not shown as a join."""
    combined = combine_metadatas(("Deezer", release(("JAY Z", "guest"))), base=release(("JAŸ-Z", "main")))

    assert credits(combined) == [[("JAŸ-Z", "main")]]
    out = capsys.readouterr().out
    assert "Read as one artist" not in out


def test_names_read_as_one_artist_beyond_case_accents_and_punctuation_are_shown(capsys: pytest.CaptureFixture) -> None:
    combine_metadatas(
        ("MusicBrainz", release(("The Herbaliser", "main"))),
        ("Discogs", credited_as(release(("The Herbaliser", "main")), "The Herbaliser", "main", "Herbalizer")),
        ("Deezer", release(("HERBALISER", "main"))),
        base=release(("Herbaliser", "main")),
    )

    out = capsys.readouterr().out
    assert "Read as one artist:\n  Herbaliser: The Herbaliser (MusicBrainz, Discogs)\n" in out


def test_a_duo_and_one_person_of_their_names_are_read_as_one_artist_and_shown(capsys: pytest.CaptureFixture) -> None:
    """The key's known cost: "Peter and Gordon" and "Peter Gordon" are one key, so salmon says so."""
    files = two_tracks([("Peter and Gordon", "main")], [("Peter Gordon", "main")])

    combine_metadatas(("Deezer", deepcopy(files)), base=files)

    out = capsys.readouterr().out
    assert "Read as one artist:\n  Peter and Gordon: Peter Gordon (the files, Deezer)\n" in out


def test_a_store_alone_shows_the_names_it_reads_as_one_artist(capsys: pytest.CaptureFixture) -> None:
    combined = combine_metadatas(
        ("Qobuz", two_tracks([("Miles Davis Quintet", "main")], [("The Miles Davis Quintet", "main")]))
    )

    assert credits(combined) == [[("Miles Davis Quintet", "main")], [("Miles Davis Quintet", "main")]]
    out = capsys.readouterr().out
    assert "Read as one artist:\n  Miles Davis Quintet: The Miles Davis Quintet (Qobuz)\n" in out


def test_a_scrape_used_alone_shows_the_names_it_reads_as_one_artist(capsys: pytest.CaptureFixture) -> None:
    meta = release()
    meta["tracks"] = make_tracks({"1": ["1", "2", "3"]}, isrc_prefix=None)
    for num, name in (("1", "Miles Davis Quintet"), ("2", "The Miles Davis Quintet"), ("3", "The Miles Davis Quintet")):
        meta["tracks"]["1"][num]["artists"] = [(name, "main")]

    decide_alone(meta, "Qobuz")

    assert credits(meta) == [[("Miles Davis Quintet", "main")]] * 3
    out = capsys.readouterr().out
    assert "Read as one artist:\n  Miles Davis Quintet: The Miles Davis Quintet (Qobuz)\n" in out


def test_names_discogs_credits_under_one_another_on_a_track_are_one_artist() -> None:
    """Discogs credits "Bob Smith" as "Bobby Smith", and "Bobby Smith" as the files' "Rob Smith": all three are one."""
    discogs = release(("Bob Smith", "main"), ("Bobby Smith", "remixer"))
    credited_as(credited_as(discogs, "Bob Smith", "main", "Bobby Smith"), "Bobby Smith", "remixer", "Rob Smith")

    combined = combine_metadatas(("Discogs", discogs), base=release(("Rob Smith", "main")))

    assert credits(combined) == [[("Rob Smith", "main"), ("Rob Smith", "remixer")]]


def test_a_duo_written_with_an_ampersand_and_one_person_of_their_names_are_shown() -> None:
    """ "Peter & Gordon" and "Peter Gordon" are one by `comparable` too, but never were across tracks."""
    files = two_tracks([("Peter & Gordon", "main")], [("Peter Gordon", "main")])
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        combine_metadatas(("Deezer", deepcopy(files)), base=files)

    assert "Read as one artist:\n  Peter & Gordon: Peter Gordon (the files, Deezer)\n" in out.getvalue()


def test_a_name_only_a_dropped_credit_gave_is_not_shown(capsys: pytest.CaptureFixture) -> None:
    """Discogs' "Bob Smith", credited as "Bobby Smith", is a fragment of "Bob Smith Band" and dropped: nothing of it
    is read as one artist."""
    discogs = credited_as(
        release(("Bob Smith Band", "main"), ("Bob Smith", "guest")), "Bob Smith", "guest", "Bobby Smith"
    )

    combined = combine_metadatas(("Discogs", discogs), base=release(("Bob Smith Band", "main")))

    assert credits(combined) == [[("Bob Smith Band", "main")]]
    out = capsys.readouterr().out
    assert "Read as one artist" not in out


def test_spellings_one_by_comparable_are_shown_once(capsys: pytest.CaptureFixture) -> None:
    """Discogs credits Andrew Watt as "WATT", the spelling Deezer and Qobuz give (one by comparable): one line."""
    combine_metadatas(
        ("Deezer", release(("WATT", "guest"))),
        ("Qobuz", release(("Watt", "guest"))),
        ("Discogs", credited_as(release(("Andrew Watt", "guest")), "Andrew Watt", "guest", "WATT")),
        base=release(("Andrew Watt", "main")),
    )

    out = capsys.readouterr().out
    assert "Read as one artist:\n  Andrew Watt: WATT (Deezer, Qobuz)\n" in out


def test_an_artist_and_their_own_remix_spelled_with_the_article_keep_both_credits() -> None:
    """The files' main "Golden Lanterns" is no fragment of the title's remixer "The Golden Lanterns": one artist."""
    deezer = release(("The Golden Lanterns", "main"))
    deezer["tracks"]["1"]["1"]["title"] = "Song (The Golden Lanterns Remix)"

    combined = combine_metadatas(("Deezer", deezer), base=release(("Golden Lanterns", "main")))

    assert credits(combined) == [[("Golden Lanterns", "main"), ("Golden Lanterns", "remixer")]]


def test_a_stated_main_beside_a_store_s_guest_spelled_with_the_article_is_kept_and_disputed(
    capsys: pytest.CaptureFixture,
) -> None:
    combined = combine_metadatas(("Deezer", release(("The Herbaliser", "guest"))), base=release(("Herbaliser", "main")))

    assert credits(combined) == [[("Herbaliser", "main")]]
    out = capsys.readouterr().out
    assert "\n  Herbaliser (guest at Deezer)\n" in out


def test_a_featuring_clause_naming_a_guest_as_a_store_spells_them_goes_from_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guest is respelled to the files' "Herbaliser"; the title's "feat. The Herbaliser" still names them."""
    formatting = msgspec.structs.replace(cfg.upload.formatting, strip_useless_versions=True)
    monkeypatch.setattr(cfg.upload, "formatting", formatting)
    files = two_tracks([("Herbaliser", "main")], [("Alice", "main")])
    files["tracks"]["1"]["2"]["title"] = "Song (feat. The Herbaliser)"
    deezer = two_tracks([("Herbaliser", "main")], [("Alice", "main"), ("The Herbaliser", "guest")])
    deezer["tracks"]["1"]["2"]["title"] = "Song (feat. The Herbaliser)"

    combined = combine_metadatas(("Deezer", deezer), base=files)

    track = combined["tracks"]["1"]["2"]
    assert (track["title"], track["artists"]) == ("Song", [("Alice", "main"), ("Herbaliser", "guest")])


def test_a_remixer_a_store_credits_under_another_spelling_is_still_written_to_remixer() -> None:
    deezer = release(("Alice", "main"), ("The Lantern Club", "remixer"))
    deezer["tracks"]["1"]["1"]["credited_remixers"] = ["The Lantern Club"]

    combined = combine_metadatas(("Deezer", deezer), base=release(("Alice", "main"), ("Lantern Club", "remixer")))

    assert create_remixer_list(combined["tracks"]["1"]["1"]) == ["Lantern Club"]


def test_a_scrape_of_discogs_used_alone_reads_its_credited_names_as_one_artist() -> None:
    """As with Discogs as the base of a merge: "Bob Smith", credited as "Bobby Smith", is the guest "Bobby Smith"."""
    meta = credited_as(release(("Bob Smith", "main"), ("Bobby Smith", "guest")), "Bob Smith", "main", "Bobby Smith")

    decide_alone(meta, "Discogs")

    assert credits(meta) == [[("Bob Smith", "main")]]


def test_as_before_names_one_by_comparable_but_not_by_the_key_are_matched_by_the_merge() -> None:
    """ "TheDream" keeps its article (no space or dash after it), but the merge still matches it to "The-Dream"."""
    combined = combine_metadatas(("Deezer", release(("The-Dream", "main"))), base=release(("TheDream", "main")))

    assert credits(combined) == [[("TheDream", "main")]]


def test_a_joined_artist_is_spelled_as_its_first_credit_in_track_order_whatever_the_tracks_order() -> None:
    tracks = {"1": {"2": one_track([("The Lanterns", "main")], "Two")["1"]["1"]}}
    tracks["1"]["1"] = one_track([("Lanterns", "main")], "One")["1"]["1"]

    _decide_credits(tracks, {}, "files")

    assert [tracks["1"][num]["artists"] for num in ("1", "2")] == [[("Lanterns", "main")]] * 2


def test_as_before_a_featuring_clause_naming_a_producer_discogs_credits_as_the_main_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discogs credits the producer Thomas Bangalter as "Daft Punk": the title's "feat. Thomas Bangalter" names no
    performer, so it stays."""
    formatting = msgspec.structs.replace(cfg.upload.formatting, strip_useless_versions=True)
    monkeypatch.setattr(cfg.upload, "formatting", formatting)
    files = release(("Daft Punk", "main"))
    files["tracks"]["1"]["1"]["title"] = "Song (feat. Thomas Bangalter)"
    discogs = credited_as(
        release(("Daft Punk", "main"), ("Thomas Bangalter", "producer")), "Thomas Bangalter", "producer", "Daft Punk"
    )
    discogs["tracks"]["1"]["1"]["title"] = "Song (feat. Thomas Bangalter)"

    combined = combine_metadatas(("Discogs", discogs), base=files)

    assert combined["tracks"]["1"]["1"]["title"] == "Song (feat. Thomas Bangalter)"


def test_a_featuring_clause_naming_a_guest_as_discogs_credits_them_goes_from_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formatting = msgspec.structs.replace(cfg.upload.formatting, strip_useless_versions=True)
    monkeypatch.setattr(cfg.upload, "formatting", formatting)
    files = release(("Alice", "main"))
    files["tracks"]["1"]["1"]["title"] = "Song (feat. Herbalizer)"
    discogs = credited_as(
        release(("Alice", "main"), ("The Herbaliser", "guest")), "The Herbaliser", "guest", "Herbalizer"
    )
    discogs["tracks"]["1"]["1"]["title"] = "Song (feat. Herbalizer)"

    combined = combine_metadatas(("Discogs", discogs), base=files)

    assert combined["tracks"]["1"]["1"]["title"] == "Song"


def test_a_featuring_clause_naming_a_guest_as_discogs_used_alone_credits_them_goes_from_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As with Discogs merged as a store: a lone Discogs scrape's own claims carry the names it credits them as."""
    formatting = msgspec.structs.replace(cfg.upload.formatting, strip_useless_versions=True)
    monkeypatch.setattr(cfg.upload, "formatting", formatting)
    meta = credited_as(release(("Alice", "main"), ("The Herbaliser", "guest")), "The Herbaliser", "guest", "Herbalizer")
    meta["tracks"]["1"]["1"]["title"] = "Song (feat. Herbalizer)"

    decide_alone(meta, "Discogs")

    assert meta["tracks"]["1"]["1"]["title"] == "Song"


def test_a_stated_credit_discogs_credits_under_a_longer_name_is_no_fragment_of_it() -> None:
    """Discogs credits Roger Taylor, producer, as the files' guest "Taylor": one artist, so the guest stays."""
    discogs = credited_as(
        release(("Queen", "main"), ("Roger Taylor", "producer")), "Roger Taylor", "producer", "Taylor"
    )

    combined = combine_metadatas(("Discogs", discogs), base=release(("Queen", "main"), ("Taylor", "guest")))

    assert credits(combined) == [[("Queen", "main"), ("Taylor", "guest"), ("Taylor", "producer")]]


@pytest.mark.parametrize("source_url", [None, DISCOGS_FIRST], ids=["by preference", "Discogs chosen"])
def test_a_stated_credit_discogs_credits_under_a_longer_name_stays_whichever_store_comes_first(
    source_url: str | None,
) -> None:
    """By preference MusicBrainz merges first, with "Roger Taylor" and no credited name: the files' guest "Taylor"
    waits for Discogs' "Taylor" before the fragment check weighs it."""
    discogs = credited_as(
        release(("Queen", "main"), ("Roger Taylor", "producer")), "Roger Taylor", "producer", "Taylor"
    )
    musicbrainz = release(("Queen", "main"), ("Roger Taylor", "producer"))

    combined = combine_metadatas(
        ("MusicBrainz", musicbrainz),
        ("Discogs", discogs),
        base=release(("Queen", "main"), ("Taylor", "guest")),
        source_url=source_url,
    )

    assert credits(combined) == [[("Queen", "main"), ("Taylor", "guest"), ("Taylor", "producer")]]


def test_a_name_credited_through_a_chain_of_discogs_names_is_no_fragment() -> None:
    """Discogs credits Robert Taylor as "Bob Taylor", and Bob Taylor as "Taylor": the files' "Taylor" is one artist
    with both, so it is no fragment of "Robert Taylor"."""
    discogs = release(("Queen", "main"), ("Robert Taylor", "producer"), ("Bob Taylor", "remixer"))
    discogs = credited_as(
        credited_as(discogs, "Robert Taylor", "producer", "Bob Taylor"), "Bob Taylor", "remixer", "Taylor"
    )

    combined = combine_metadatas(("Discogs", discogs), base=release(("Queen", "main"), ("Taylor", "guest")))

    assert ("Taylor", "guest") in credits(combined)[0]


def test_as_before_two_artists_whose_names_look_alike_stay_two(capsys: pytest.CaptureFixture) -> None:
    """On two tracks: on one, the merge's fragment check drops "Santana" beside "Carlos Santana" (#1112)."""
    files = two_tracks([("Stevie Nicks", "main"), ("Carlos Santana", "main")], [("Santana", "main")])
    deezer = two_tracks([("Stevie Nicks", "main"), ("Stevie Wonder", "guest")], [("Santana", "main")])

    combined = combine_metadatas(("Deezer", deezer), base=files)

    assert credits(combined) == [
        [("Stevie Nicks", "main"), ("Carlos Santana", "main"), ("Stevie Wonder", "guest")],
        [("Santana", "main")],
    ]
    out = capsys.readouterr().out
    assert "Read as one artist" not in out


def test_as_before_a_review_keeps_two_spellings_the_user_kept_apart() -> None:
    """The list a review save rebuilds from the tracks keys names as before: the decision has already joined what the
    sources gave, so two spellings left are the user's."""
    tracks = two_tracks([("Lab Rats", "main")], [("The Lab Rats", "main")])["tracks"]

    artists, _ = generate_artists(tracks)

    assert artists == [("Lab Rats", "main"), ("The Lab Rats", "main")]


def test_as_before_a_name_only_discogs_credits_an_artist_as_is_not_shown(capsys: pytest.CaptureFixture) -> None:
    """Discogs credits Larry Dunn as "L. Dunn", but no source gives that name: nothing is read as one artist."""
    discogs = credited_as(release(("Larry Dunn", "main")), "Larry Dunn", "main", "L. Dunn")

    combine_metadatas(("Discogs", discogs), base=release(("Larry Dunn", "main")))

    out = capsys.readouterr().out
    assert "Read as one artist" not in out


def deezer(*songs: tuple[str, dict]) -> dict:
    """The release Deezer's parser makes of these songs, each a title and its SNG_CONTRIBUTORS."""
    meta = release()
    meta["tracks"] = assign_track_totals(
        deezer_tracks(*(deezer_song(n, title=title, contributors=c) for n, (title, c) in enumerate(songs, 1)))
    )
    return meta


def files(*tracks: tuple[str, list[tuple[str, str]]]) -> dict:
    """Files tagged as deemix tags them: every name in a track's ARTISTS, its remixers too, as ARTIST."""
    meta = release()
    meta["tracks"] = make_tracks({"1": [str(n) for n in range(1, len(tracks) + 1)]}, isrc_prefix=None)
    for n, (title, artists) in enumerate(tracks, 1):
        meta["tracks"]["1"][str(n)].update(title=title, artists=artists)
    return meta


def test_deezer_as_the_only_store_over_deemix_files_credits_their_remixer_as_remixer_not_main() -> None:
    """deemix writes a remixer as ARTIST; Deezer states them remixer, so the release names them remixer only
    (#1132: Remixes 81>04, Daft Club and four other famous remix albums get their ALBUMARTIST back)."""
    base = files(("Alejandro (Guéna LG Remix)", [("Lady Gaga", "main"), ("Guéna LG", "main")]))
    store = deezer(("Alejandro (Guéna LG Remix)", {"main_artist": ["Lady Gaga"], "remixer": ["Guéna LG"]}))

    combined = clean_metadata(combine_metadatas(("Deezer", store), base=base))

    track = combined["tracks"]["1"]["1"]
    assert track["artists"] == [("Lady Gaga", "main"), ("Guéna LG", "remixer")]
    assert create_remixer_list(track) == ["Guéna LG"]


def test_as_before_deezer_as_the_only_store_reads_a_title_s_remixer_on_a_track_where_it_states_none() -> None:
    """A release stating a remixer on one track and naming one only in another's title keeps both (#1125)."""
    store = deezer(
        ("Song (Bob Remix)", {"main_artist": ["Alice"], "remixer": ["Bob"]}),
        ("Other (Carol Remix)", {"main_artist": ["Alice"]}),
    )

    combined = combine_metadatas(("Deezer", store), base=files(("Song (Bob Remix)", []), ("Other (Carol Remix)", [])))

    assert ("Bob", "remixer") in combined["tracks"]["1"]["1"]["artists"]
    assert ("Carol", "remixer") in combined["tracks"]["1"]["2"]["artists"]


def test_a_dj_deezer_states_as_main_and_remixer_is_only_remixer_on_that_track() -> None:
    """Accepted for now (#1132): The Blessed Madonna mixes Club Future Nostalgia and remixes two of its tracks; on
    those two the release names her remixer only, as for any remixer the files give as ARTIST."""
    both = [("Dua Lipa", "main"), ("The Blessed Madonna", "main")]
    base = files(("Love Again", both), ("Levitating", both))
    store = deezer(
        ("Love Again", {"main_artist": ["Dua Lipa", "The Blessed Madonna"]}),
        ("Levitating", {"main_artist": ["Dua Lipa", "The Blessed Madonna"], "remixer": ["The Blessed Madonna"]}),
    )

    combined = clean_metadata(combine_metadatas(("Deezer", store), base=base))

    assert credits(combined) == [both, [("Dua Lipa", "main"), ("The Blessed Madonna", "remixer")]]


def test_deezer_s_stated_guest_is_kept_over_qobuz_s_stated_main_with_no_files() -> None:
    """Accepted for now (#1132): on an "X with Y" album (Monk with Coltrane) both should be main once every store
    states its credits; with no files Deezer is the base, and the first stated claim wins."""
    store = deezer(("Ruby, My Dear", {"main_artist": ["Thelonious Monk"], "featuring": ["John Coltrane"]}))
    qobuz = release(("Thelonious Monk", "main"), ("John Coltrane", "main"))

    combined = combine_metadatas(("Qobuz", stating(qobuz)), ("Deezer", store))

    assert combined["tracks"]["1"]["1"]["artists"] == [("Thelonious Monk", "main"), ("John Coltrane", "guest")]


def scraped_alone(monkeypatch: pytest.MonkeyPatch, *songs: dict) -> dict:
    """A lone Deezer scrape (salmon meta, the web lookup) of these songs, scraped and decided as run_metadata does."""
    soup = {
        "id": 1,
        "title": "Album",
        "upc": "1",
        "cover_xl": None,
        "genres": {"data": []},
        "label": "Label",
        "release_date": "2026-01-01",
        "record_type": "album",
        "tracklist": list(songs),
    }
    scraper = Deezer()

    async def fetch_data(*_args: object, **_kwargs: object) -> dict:
        return deepcopy(soup)

    monkeypatch.setattr(scraper, "fetch_data", fetch_data)
    alone = anyio.run(scraper.scrape_release, "https://www.deezer.com/album/1")
    decide_alone(alone, "Deezer")
    return alone


def test_a_lone_deezer_scrape_keeps_its_stated_remixer_and_names_the_remix_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remixer Deezer states is the track's REMIXER, and its title is written from Deezer's title and version,
    not given the remix again."""
    contributors = {"main_artist": ["Lady Gaga"], "remixer": ["Guéna LG"]}
    alone = scraped_alone(monkeypatch, deezer_song(1, "(Guéna LG Remix)", "Alejandro", contributors))

    track = alone["tracks"]["1"]["1"]
    assert track["artists"] == [("Lady Gaga", "main"), ("Guéna LG", "remixer")]
    assert track["title"] == "Alejandro (Guéna LG Remix)"
    assert create_remixer_list(track) == ["Guéna LG"]


def test_a_lone_deezer_scrape_leads_with_its_main_not_a_title_guest_it_states_as_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alone = scraped_alone(
        monkeypatch,
        deezer_song(1, title="Song (feat. Bob)", contributors={"main_artist": ["Alice", "Bob"]}),
        deezer_song(2, title="Other"),
    )

    assert alone["artists"][0] == ("Alice", "main")
    assert alone["tracks"]["1"]["1"]["artists"][0] == ("Alice", "main")


def test_a_guest_only_deezer_s_title_names_yields_to_qobuz_s_stated_main_after_the_main() -> None:
    """Deezer's title guest is its guess, so with no files Qobuz's stated main wins, though Deezer comes first; and
    it follows Deezer's main, as the guess is claimed after it."""
    store = deezer(("Song (feat. Bob)", {"main_artist": ["Alice"]}))
    qobuz = release(("Alice", "main"), ("Bob", "main"))

    combined = combine_metadatas(("Qobuz", stating(qobuz)), ("Deezer", store))

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "main")]
