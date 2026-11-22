"""The merge's credits decided from each source's claims (ADR 0007): who each claim comes from, the spellings, and
the disputes salmon prints."""

import contextlib
import io
from copy import deepcopy

import msgspec
import pytest
from helpers.tagger_combine import make_tracks, one_track, stating, stores_stating

from salmon import cfg
from salmon.common.credits import Claim
from salmon.tagger.combine import _decide_credits, combine_metadatas, combine_tracks, decide_alone
from salmon.tagger.retagger import create_remixer_list
from salmon.tagger.sources.base import generate_artists


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
    assert "Read as one artist" not in capsys.readouterr().out


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
    assert "Read as one artist:\n  Miles Davis Quintet: The Miles Davis Quintet (Qobuz)\n" in capsys.readouterr().out


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
    assert "Read as one artist" not in capsys.readouterr().out


def test_spellings_one_by_comparable_are_shown_once(capsys: pytest.CaptureFixture) -> None:
    """Discogs credits Andrew Watt as "WATT", the spelling Deezer and Qobuz give (one by comparable): one line."""
    combine_metadatas(
        ("Deezer", release(("WATT", "guest"))),
        ("Qobuz", release(("Watt", "guest"))),
        ("Discogs", credited_as(release(("Andrew Watt", "guest")), "Andrew Watt", "guest", "WATT")),
        base=release(("Andrew Watt", "main")),
    )

    assert "Read as one artist:\n  Andrew Watt: WATT (Deezer, Qobuz)\n" in capsys.readouterr().out


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
    assert "\n  Herbaliser (guest at Deezer)\n" in capsys.readouterr().out


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
    assert "Read as one artist" not in capsys.readouterr().out


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

    assert "Read as one artist" not in capsys.readouterr().out
