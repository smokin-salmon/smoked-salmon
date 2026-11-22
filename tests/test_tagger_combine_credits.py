"""The merge's credits decided from each source's claims (ADR 0007): who each claim comes from, the spellings, and
the disputes salmon prints."""

from copy import deepcopy

import pytest
from helpers.tagger_combine import make_tracks, one_track, stating, stores_stating
from test_common_credits import files_claim, store_claim

from salmon.common.credits import Claim
from salmon.tagger.combine import _decide_credits, _join_split_names, combine_metadatas, combine_tracks, decide_alone
from salmon.tagger.sources.base import construct_replacement_list


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


def test_as_before_one_title_naming_two_spellings_of_a_remixer_keeps_both() -> None:
    files, deezer = release(("Bob", "main")), release(("Bob", "main"))
    for meta in (files, deezer):
        meta["tracks"]["1"]["1"]["title"] = "Song (Jay-Z & Jay Z Remix)"

    combined = combine_metadatas(("Deezer", deezer), base=files)

    assert combined["tracks"]["1"]["1"]["artists"] == [("Bob", "main"), ("Jay-Z", "remixer"), ("Jay Z", "remixer")]


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
        ("Discogs", "K-Ci and JoJo", "K-Ci and JoJo", "main", True),
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
