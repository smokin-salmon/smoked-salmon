"""The merge's credits decided from each source's claims (ADR 0007): who each claim comes from, the spellings, and
the disputes salmon prints."""

from copy import deepcopy

import pytest
from helpers.tagger_combine import make_tracks, one_track

from salmon.common.credits import Claim
from salmon.tagger.combine import _decide_credits, combine_metadatas, combine_tracks


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
    assert "Kept the files' main where stores say guest:\n  Bob (guest at Qobuz)\n" in capsys.readouterr().out


def test_the_files_guest_is_kept_where_a_store_says_main(capsys: pytest.CaptureFixture) -> None:
    combine_metadatas(
        ("Deezer", release(("Alice", "main"), ("Bob", "main"))), base=release(("Alice", "main"), ("Bob", "guest"))
    )

    assert "Kept the files' guest where stores say main:\n  Bob (main at Deezer)\n" in capsys.readouterr().out


def test_with_no_files_the_first_store_is_the_base(capsys: pytest.CaptureFixture) -> None:
    combined = combine_metadatas(
        ("Qobuz", release(("Alice", "main"), ("Bob", "guest"))), ("Deezer", release(("Alice", "main"), ("Bob", "main")))
    )

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "main")]
    assert "Kept Deezer's main where other stores say guest:\n  Bob (guest at Qobuz)\n" in capsys.readouterr().out


def test_names_the_same_stores_dispute_share_a_line_whatever_the_order(capsys: pytest.CaptureFixture) -> None:
    files = two_tracks([("Bob", "main"), ("Carol", "main")], [("Carol", "main")])
    deezer = two_tracks([("Bob", "guest")], [("Carol", "guest")])
    qobuz = two_tracks([("Bob", "guest"), ("Carol", "guest")], [])

    combine_metadatas(("Qobuz", qobuz), ("Deezer", deezer), base=deepcopy(files))

    assert "\n  Bob, Carol (guest at Deezer, Qobuz)\n" in capsys.readouterr().out


def test_a_store_credit_the_merge_matches_to_the_base_is_one_name_to_dispute(capsys: pytest.CaptureFixture) -> None:
    """Qobuz's "JAY Z" is the merge's match for Deezer's "Jay-Z" (`comparable`), so it disputes the files' main too."""
    combine_metadatas(
        ("Qobuz", release(("JAY Z", "guest"))), ("Deezer", release(("Jay-Z", "guest"))), base=release(("Jay-Z", "main"))
    )

    assert "\n  Jay-Z (guest at Deezer, Qobuz)\n" in capsys.readouterr().out


def test_a_name_spelled_two_ways_is_one_name_to_dispute(capsys: pytest.CaptureFixture) -> None:
    combine_metadatas(("Deezer", release(("Kool & the Gang", "guest"))), base=release(("Kool & The Gang", "main")))

    assert "\n  Kool & the Gang (guest at Deezer)\n" in capsys.readouterr().out


def test_as_before_nothing_is_printed_when_the_sources_agree(capsys: pytest.CaptureFixture) -> None:
    combine_metadatas(("Qobuz", release(("Alice", "main"))), base=release(("Alice", "main")))

    assert "Kept" not in capsys.readouterr().out


def test_as_before_a_name_takes_the_spelling_with_fewer_capitals() -> None:
    files = release(("Kool & The Gang", "main"))

    combined = combine_metadatas(("Deezer", release(("Kool & the Gang", "guest"))), base=files)

    assert combined["tracks"]["1"]["1"]["artists"] == [("Kool & the Gang", "main")]


def test_as_before_a_fragment_the_merge_drops_stays_dropped() -> None:
    combined = combine_metadatas(("Deezer", release(("Ava", "main"))), base=release(("Ava Daniel", "main")))

    assert combined["tracks"]["1"]["1"]["artists"] == [("Ava Daniel", "main")]


def test_as_before_one_title_naming_two_spellings_of_a_remixer_keeps_both() -> None:
    files, deezer = release(("Bob", "main")), release(("Bob", "main"))
    for meta in (files, deezer):
        meta["tracks"]["1"]["1"]["title"] = "Song (Jay-Z & Jay Z Remix)"

    combined = combine_metadatas(("Deezer", deezer), base=files)

    assert combined["tracks"]["1"]["1"]["artists"] == [("Bob", "main"), ("Jay-Z", "remixer"), ("Jay Z", "remixer")]


def test_as_before_a_store_alone_keeps_its_credits(capsys: pytest.CaptureFixture) -> None:
    combined = combine_metadatas(("Deezer", release(("Alice", "main"), ("Bob", "guest"))))

    assert combined["tracks"]["1"]["1"]["artists"] == [("Alice", "main"), ("Bob", "guest")]
    assert "Kept" not in capsys.readouterr().out


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
