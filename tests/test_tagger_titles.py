"""A track's title and its version (#869 step 3b, #1077): kept apart by the stores that give them apart, and written
once, after the last store is merged, from the files' title and what each store gave beside it."""

import random
import unicodedata
from collections.abc import Sequence
from copy import deepcopy

import anyio
import msgspec
import pytest
from test_tagger_combine import make_metadata

from salmon import cfg
from salmon.tagger.combine import (
    _pair_base_tracks,
    _written_title,
    combine_metadatas,
    combine_tracks,
    decide_alone,
)
from salmon.tagger.retagger import order_tracks_for_files
from salmon.tagger.sources import MERGE_ONLY_KEYS
from salmon.tagger.sources.base import MetadataMixin, title_with_version
from salmon.tagger.sources.deezer import Scraper as Deezer
from salmon.tagger.sources.qobuz import Scraper as Qobuz


def setting(monkeypatch: pytest.MonkeyPatch, strip: bool) -> None:
    formatting = msgspec.structs.replace(cfg.upload.formatting, strip_useless_versions=strip)
    monkeypatch.setattr(cfg.upload, "formatting", formatting)


def release(*titles: str | tuple[str, str | None], replay_gain: str | None = None) -> dict:
    """A release whose tracks have these titles, a (title, version) pair for a store that gives the version apart."""
    meta = make_metadata("Album")
    template = meta["tracks"]["1"]["1"]
    meta["tracks"]["1"] = {}
    for number, given in enumerate(titles, 1):
        title, version = given if isinstance(given, tuple) else (given, None)
        track = {**deepcopy(template), "track#": str(number), "title": title, "replay_gain": replay_gain}
        if isinstance(given, tuple):
            track["version"] = version
        meta["tracks"]["1"][str(number)] = track
    return meta


def written(*sources: tuple[str, dict], files: dict | None = None) -> list[str]:
    merged = combine_metadatas(*deepcopy(sources), base=deepcopy(files))
    return [track["title"] for track in merged["tracks"]["1"].values()]


def test_as_before_a_store_s_version_is_kept_with_the_setting_off_and_never_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the setting on, test_tagger_useless_versions covers what is dropped."""
    setting(monkeypatch, strip=False)

    assert MetadataMixin.parse_title("Django", "(Original Mix)") == "Django (Original Mix)"
    assert MetadataMixin.parse_title("Song (Live)", "Live") == "Song (Live)"


def deezer_song(number: int, version: str, title: str = "Spellbound") -> dict:
    """A track of Deezer's album page."""
    return {
        "DISK_NUMBER": "1",
        "TRACK_NUMBER": str(number),
        "SNG_TITLE": title,
        "VERSION": version,
        "SNG_CONTRIBUTORS": {"main_artist": ["Alice"]},
        "ARTISTS": [],
        "ISRC": None,
        "EXPLICIT_LYRICS": "0",
        "SNG_ID": str(number),
    }


# The stores


def test_qobuz_gives_a_track_s_version_apart_from_its_title() -> None:
    data = {
        "artist": {"name": "Alice"},
        "artists": [{"name": "Alice", "roles": ["main-artist"]}],
        "tracks": {
            "items": [
                {"track_number": 1, "title": "Whole Lotta Love ", "version": "Remaster"},
                {"track_number": 2, "title": "Living Loving Maid", "version": None},
            ]
        },
    }

    tracks = anyio.run(Qobuz().parse_tracks, data)["1"]

    assert [(t["title"], t["version"]) for t in tracks.values()] == [
        ("Whole Lotta Love", "Remaster"),
        ("Living Loving Maid", None),
    ]


def test_deezer_gives_a_track_s_version_apart_from_its_title_without_its_brackets() -> None:
    tracks = anyio.run(
        Deezer().parse_tracks, {"tracklist": [deezer_song(1, "(Mixed)"), deezer_song(2, ""), deezer_song(3, "(A) (B)")]}
    )["1"]

    assert [(t["title"], t["version"]) for t in tracks.values()] == [
        ("Spellbound", "Mixed"),
        ("Spellbound", None),
        ("Spellbound", "(A) (B)"),
    ]


def test_a_release_of_remixes_given_apart_is_still_a_remix_release() -> None:
    meta = release(*((f"Song {n}", f"DJ {n} Remix") for n in range(1, 9)))
    meta["tracks"]["1"] = {n: {**t, "tracktotal": "8"} for n, t in meta["tracks"]["1"].items()}

    _, rls_type = Deezer().determine_rls_type(meta)

    assert rls_type == "Remix"


def test_as_before_original_mixes_given_apart_do_not_make_a_remix_release() -> None:
    meta = release(*((f"Song {n}", "Original Mix") for n in range(1, 9)))

    _, rls_type = Deezer().determine_rls_type(meta)

    assert rls_type != "Remix"


# The merge


@pytest.mark.parametrize(
    ("files", "first", "second"),
    [
        ("Zzz", "Alpha", "Beta"),
        ("Don't Stop Me Now", "Don’t Stop Me Now", "Dont Stop Me Now"),
        ("Song One", "Song one", "SONG ONE"),
    ],
)
def test_the_files_title_is_written_when_they_carry_no_replay_gain(files: str, first: str, second: str) -> None:
    """#1077's rows: a later store's title no longer replaces the files'."""
    titles = written(("Deezer", release(first)), ("Discogs", release(second)), files=release(files))

    assert titles == [files]


@pytest.mark.parametrize(("files", "first", "second"), [("Zzz", "Alpha", "Beta"), ("Song One", "Song one", "SONG ONE")])
def test_as_before_the_files_title_is_written_when_they_carry_replay_gain(files: str, first: str, second: str) -> None:
    titles = written(
        ("Deezer", release(first)), ("Discogs", release(second)), files=release(files, replay_gain="-7.0 dB")
    )

    assert titles == [files]


def test_the_files_title_keeps_its_own_version_and_takes_a_store_s_accents() -> None:
    titles = written(
        ("Qobuz", release(("Ný batterí", "Remastered 2023"))), files=release("Ny Batteri (Remastered 2023)")
    )

    assert titles == ["Ný Batterí (Remastered 2023)"]


def test_a_placeholder_files_title_takes_the_first_store_s_title_and_version() -> None:
    titles = written(
        ("Deezer", release(("Spellbound", "Mixed"))), ("Discogs", release("Spellbound")), files=release("Track 01")
    )

    assert titles == ["Spellbound (Mixed)"]


def test_without_files_the_first_store_s_title_and_version_are_written() -> None:
    titles = written(("Deezer", release(("Cold Heart", "PNAU Remix"))), ("Discogs", release("Cold Heart")))

    assert titles == ["Cold Heart (PNAU Remix)"]


def test_a_featuring_clause_qobuz_gives_as_a_version_is_not_written() -> None:
    """Settle's "F For You", version "ft. Mary J Blige": the guest is a credit, which guests_in_track_title writes its
    own way."""
    data = {
        "artist": {"name": "Disclosure"},
        "artists": [{"name": "Disclosure", "roles": ["main-artist"]}],
        "tracks": {
            "items": [
                {
                    "track_number": 1,
                    "title": "F For You",
                    "version": "ft. Mary J Blige",
                    "performers": "Disclosure, MainArtist - Mary J. Blige, FeaturedArtist",
                }
            ]
        },
    }
    qobuz = release("F For You")
    qobuz["tracks"] = anyio.run(Qobuz().parse_tracks, data)

    decide_alone(qobuz, "Qobuz")

    assert qobuz["tracks"]["1"]["1"]["title"] == "F For You"


def test_the_merge_leaves_no_merge_only_key_behind_and_a_lone_scrape_is_written_whole() -> None:
    """run_metadata drops a lone scrape's merge-only keys once decide_alone has written its titles."""
    merged = combine_metadatas(
        ("Qobuz", release(("Song", "X Remix"))), ("Discogs", release("Song")), base=release("Track 01")
    )
    alone = release(("Song", "X Remix"))
    decide_alone(alone, "Deezer")

    assert not set(MERGE_ONLY_KEYS) & merged["tracks"]["1"]["1"].keys()
    assert alone["tracks"]["1"]["1"]["title"] == "Song (X Remix)"


def test_as_before_the_merge_reads_a_remixer_from_deezer_s_version() -> None:
    """Deezer credits no remixer: the merge reads them from its title and version, as from its folded title before."""
    deezer = release("Song")
    deezer["tracks"] = anyio.run(Deezer().parse_tracks, {"tracklist": [deezer_song(1, "(Alice Remix)", "Song")]})
    merged = combine_metadatas(("Deezer", deezer), base=release("Song (Alice Remix)"))

    assert ("Alice", "remixer") in merged["tracks"]["1"]["1"]["artists"]


def test_a_files_title_holding_a_store_s_version_pairs_with_that_store_s_track() -> None:
    """Deemix-shaped files fold Deezer's version into their title: "Apple (Mixed)" is Deezer's "Apple", "Mixed", even
    when the files' order is not the store's."""
    files = release("Zebra (Mixed)", "Apple (Mixed)")
    for track in files["tracks"]["1"].values():
        track["numbered"] = False
    deezer = release(("Apple", "Mixed"), ("Zebra", "Mixed"))
    deezer["tracks"]["1"]["1"]["isrc"], deezer["tracks"]["1"]["2"]["isrc"] = "APPLE", "ZEBRA"

    merged = combine_metadatas(("Deezer", deezer), base=files)

    assert {t["title"]: t["isrc"] for t in merged["tracks"]["1"].values()} == {
        "Apple (Mixed)": "APPLE",
        "Zebra (Mixed)": "ZEBRA",
    }


def test_as_before_a_featuring_version_is_folded_into_a_store_s_title_at_its_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tidal reads its guests from the title alone, so "feat. Bob" given apart stays in it."""
    setting(monkeypatch, strip=True)

    assert MetadataMixin.parse_title("Song", "feat. Bob") == "Song (feat. Bob)"


@pytest.mark.parametrize("source", ["Discogs", "MusicBrainz"])
def test_as_before_with_no_files_a_store_s_title_keeps_a_featuring_clause_naming_someone_uncredited(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """The title is then the only place the guest appears, as with the files' title."""
    setting(monkeypatch, strip=True)

    titles = written((source, release("Song (feat. Xavier)")))

    assert titles == ["Song (feat. Xavier)"]


def test_with_no_files_a_store_s_featuring_clause_naming_only_credited_guests_goes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As with the files: the credit says it already."""
    setting(monkeypatch, strip=True)
    discogs = release("Song (feat. Xavier)")
    discogs["tracks"]["1"]["1"]["artists"].append(("Xavier", "guest"))

    titles = written(("Discogs", discogs))

    assert titles == ["Song"]


def test_as_before_with_no_files_a_store_s_title_pairs_with_the_next_store_s_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deezer's "Zebra", "Remastered" is "Zebra" as written, which Discogs' "Zebra" pairs with in any order."""
    setting(monkeypatch, strip=True)
    deezer = release(("Zebra", "Remastered"), ("Apple", "Remastered"))
    deezer["tracks"]["1"]["1"]["isrc"], deezer["tracks"]["1"]["2"]["isrc"] = "ZEBRA", "APPLE"

    discogs = release("Apple", "Zebra")
    discogs["tracks"]["1"]["1"]["artists"].append(("Ann", "guest"))
    discogs["tracks"]["1"]["2"]["artists"].append(("Zed", "guest"))

    merged = combine_metadatas(("Deezer", deezer), ("Discogs", discogs))

    guests = {t["isrc"]: [n for n, r in t["artists"] if r == "guest"] for t in merged["tracks"]["1"].values()}
    assert guests == {"APPLE": ["Ann"], "ZEBRA": ["Zed"]}


def test_numbered_files_salmon_wrote_without_a_credited_featuring_clause_pair_by_their_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files a merge already wrote as "Post", for Deezer's "Post", "feat. Bob": the second title pass pairs them."""
    setting(monkeypatch, strip=True)
    deezer = release("Pre (feat. Bob)", ("Pre", "Bob Remix"), ("Post", "feat. Bob"))
    for number, isrc in (("1", "PREFEAT"), ("2", "PREREMIX"), ("3", "POST")):
        deezer["tracks"]["1"][number]["isrc"] = isrc

    merged = combine_metadatas(
        ("Deezer", deezer), base=release("Post", "Pre (Bob Remix)", "Pre", replay_gain="-7.0 dB")
    )

    assert {t["title"]: t["isrc"] for t in merged["tracks"]["1"].values()} == {
        "Post": "POST",
        "Pre (Bob Remix)": "PREREMIX",
        "Pre": "PREFEAT",
    }


def test_as_before_numbered_files_out_of_order_pair_by_the_store_s_title_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deezer's "Song", "Remastered" is "Song" as written by the setting, which the files' "Song" pairs with."""
    setting(monkeypatch, strip=True)
    deezer = release(("Song", "Remastered"), ("Song 2", "Remastered"))
    deezer["tracks"]["1"]["1"]["isrc"], deezer["tracks"]["1"]["2"]["isrc"] = "SONG", "SONG2"

    merged = combine_metadatas(("Deezer", deezer), base=release("Song 2", "Song"))

    assert {t["title"]: t["isrc"] for t in merged["tracks"]["1"].values()} == {"Song": "SONG", "Song 2": "SONG2"}


def test_unnumbered_files_holding_part_of_a_store_s_version_pair_with_its_track() -> None:
    """The files' "Apple (Mixed)" is found in Deezer's "Apple", "Mixed Edit" by the partial-title pass, as folded."""
    files = release("Zebra (Mixed)", "Apple (Mixed)")
    for track in files["tracks"]["1"].values():
        track["numbered"] = False
    deezer = release(("Apple", "Mixed Edit"), ("Zebra", "Mixed Edit"))
    deezer["tracks"]["1"]["1"]["isrc"], deezer["tracks"]["1"]["2"]["isrc"] = "APPLE", "ZEBRA"

    merged = combine_metadatas(("Deezer", deezer), base=files)

    assert {t["title"]: t["isrc"] for t in merged["tracks"]["1"].values()} == {
        "Apple (Mixed)": "APPLE",
        "Zebra (Mixed)": "ZEBRA",
    }


@pytest.mark.parametrize("files", ["Work (Original Mix)", "Work (Remastered)", "Work (Album Version (Explicit))"])
def test_the_files_title_keeps_its_own_version_whatever_the_setting(
    monkeypatch: pytest.MonkeyPatch, files: str
) -> None:
    """What the release says is not trimmed: strip_useless_versions applies to the stores' titles."""
    setting(monkeypatch, strip=True)

    titles = written(("Qobuz", release(("Work", None))), ("Discogs", release("Work")), files=release(files))

    assert titles == [files]


def test_a_store_s_version_is_not_added_to_the_files_title() -> None:
    titles = written(
        ("Deezer", release(("Song", "Alice Remix"))),
        ("Beatport", release("Song (Original Mix)")),
        files=release("Song"),
    )

    assert titles == ["Song"]


def test_a_featuring_clause_naming_credited_artists_leaves_the_files_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """The credit says it, as on a store's title."""
    setting(monkeypatch, strip=True)
    files = release("Pre (feat. Alice)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"].append(("Alice", "guest"))

    titles = written(("Deezer", release("Pre")), files=files)

    assert titles == ["Pre"]


def test_as_before_the_files_featuring_clause_stays_with_the_setting_off(monkeypatch: pytest.MonkeyPatch) -> None:
    setting(monkeypatch, strip=False)
    files = release("Pre (feat. Alice)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"].append(("Alice", "guest"))

    titles = written(("Deezer", release("Pre")), files=files)

    assert titles == ["Pre (feat. Alice)"]


def test_a_featuring_clause_naming_an_artist_the_track_doesn_t_credit_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    """It isn't redundant then: the title is the only place the guest is named."""
    setting(monkeypatch, strip=True)

    titles = written(("Discogs", release("Hard to Love")), files=release("Hard to Love (feat. Jessie Reyez)"))

    assert titles == ["Hard to Love (feat. Jessie Reyez)"]


def test_a_files_track_with_no_title_takes_the_first_store_s() -> None:
    files = release("x")
    files["tracks"]["1"]["1"]["title"] = None

    titles = written(("Qobuz", release(("Song", "Alice Remix"))), files=files)

    assert titles == ["Song (Alice Remix)"]


def test_accents_come_from_a_store_s_title_that_folds_its_version() -> None:
    titles = written(("Discogs", release("Ný batterí (Remastered)")), files=release("Ny Batteri"))

    assert titles == ["Ný Batterí"]


def test_a_featuring_clause_naming_a_credited_artist_with_a_comma_in_their_name_goes() -> None:
    files = release("Sasquatch (feat. Tyler, The Creator)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"].append(("Tyler, The Creator", "guest"))

    titles = written(("Discogs", release("Sasquatch")), files=files)

    assert titles == ["Sasquatch"]


def test_a_featuring_clause_joining_credited_artists_with_and_goes() -> None:
    files = release("Song (feat. Alice and Bob)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"] += [("Alice", "guest"), ("Bob", "guest")]

    titles = written(("Discogs", release("Song")), files=files)

    assert titles == ["Song"]


def featured(clause: str, guests: list[str]) -> list[str]:
    files = release(f"Song (feat. {clause})", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"] += [(guest, "guest") for guest in guests]
    return written(("Discogs", release("Song")), files=files)


@pytest.mark.parametrize(
    ("clause", "guests"),
    [
        ("Alice & X", ["Alice"]),  # X, credited nowhere, is a name, not a joining word
        ("X & Alice", ["Alice"]),
        ("Alice, Bob & Carol", ["Alice", "Bob"]),
        ("Alice & X & Bob", ["Alice", "Bob"]),  # an "x" beside an ampersand is a name too
        ("Alice, X & Bob", ["Alice", "Bob"]),
        ("X Alice", ["Alice"]),  # a joining word at an end joins nothing
        ("Alice & X Bob", ["Alice", "Bob"]),  # nor one after a separator: "X Bob" may be a name
    ],
)
def test_as_before_a_featuring_clause_naming_someone_uncredited_stays(clause: str, guests: list[str]) -> None:
    titles = featured(clause, guests)

    assert titles == [f"Song (feat. {clause})"]


def test_a_featuring_clause_joining_credited_artists_with_x_goes() -> None:
    """Between two credited names, "x" joins them."""
    titles = featured("Alice x Bob", ["Alice", "Bob"])

    assert titles == ["Song"]


@pytest.mark.parametrize("clause", ["M.I.A. and Bob", "Wham! x Bob", "Zoe\u0308 and Bob"])
def test_a_featuring_clause_joining_credited_artists_whose_names_end_in_a_mark_goes(clause: str) -> None:
    titles = featured(clause, [clause.split(" ")[0], "Bob"])

    assert titles == ["Song"]


def test_a_featuring_clause_joining_credited_artists_with_a_comma_and_and_goes() -> None:
    titles = featured("Alice, Bob, and Carol", ["Alice", "Bob", "Carol"])

    assert titles == ["Song"]


def test_accents_come_from_a_store_s_title_whose_version_is_not_the_files() -> None:
    titles = written(("Discogs", release("Ný batterí (Remastered)")), files=release("Ny Batteri (Live)"))

    assert titles == ["Ný Batterí (Live)"]


@pytest.mark.parametrize("store", ["Song (Björk Remix)", "Söng (Björk Remix)"])
def test_as_before_accents_in_the_files_version_are_restored_too(store: str) -> None:
    titles = written(("Discogs", release(store)), files=release("Song (Bjork Remix)"))

    assert titles == [store]


def test_accents_in_a_version_a_store_gives_apart_are_restored() -> None:
    titles = written(("Qobuz", release(("Song", "Tiësto Remix"))), files=release("Song (Tiesto Remix)"))

    assert titles == ["Song (Tiësto Remix)"]


def test_accents_in_a_version_a_store_gives_apart_in_brackets_are_restored() -> None:
    """Qobuz sends some versions in their brackets ("(Rehearsal)")."""
    titles = written(("Qobuz", release(("Song", "(Tiësto Remix)"))), files=release("Song (Tiesto Remix)"))

    assert titles == ["Song (Tiësto Remix)"]


def test_as_before_a_track_and_its_featuring_version_pair_by_their_own_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deezer's "Intro", "feat. Bob" pairs as "Intro (feat. Bob)", not as the "Intro" beside it."""
    setting(monkeypatch, strip=True)
    tracklist = [deezer_song(1, "(feat. Bob)", "Intro"), deezer_song(2, "", "Intro")]
    tracklist[0]["ISRC"], tracklist[1]["ISRC"] = "FEAT", "PLAIN"
    deezer = release("x")
    deezer["tracks"] = anyio.run(Deezer().parse_tracks, {"tracklist": tracklist})

    merged = combine_metadatas(("Deezer", deezer), base=release("Intro", "Intro (feat. Bob)", replay_gain="-7.0 dB"))

    assert {t["title"]: t["isrc"] for t in merged["tracks"]["1"].values()} == {
        "Intro": "PLAIN",
        "Intro (feat. Bob)": "FEAT",
    }


@pytest.mark.parametrize(("credited", "expected"), [({"bob"}, "Song"), (set(), "Song (feat. Bob)")])
def test_a_store_s_featuring_version_in_brackets_of_its_own_goes_only_when_credited(
    monkeypatch: pytest.MonkeyPatch, credited: set[str], expected: str
) -> None:
    setting(monkeypatch, strip=True)

    written, _why = _written_title(titled("Song", "(feat. Bob)"), False, credited)

    assert written == expected


def test_as_before_with_no_files_a_store_s_placeholder_title_takes_the_next_store_s() -> None:
    """A store's "Track 1" names no more than a ripper's: the next store's title, as the last store's was before."""
    titles = written(("Deezer", release("Track 1")), ("Qobuz", release("Intro")))

    assert titles == ["Intro"]


def test_with_no_files_any_store_s_title_follows_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lone scrape (salmon meta) or descgen: every store's title is written by strip_useless_versions, as Deezer's
    and Tidal's were."""
    setting(monkeypatch, strip=True)
    beatport = release("Work (Original Mix)")

    decide_alone(beatport, "Beatport")

    assert beatport["tracks"]["1"]["1"]["title"] == "Work"


def test_a_featuring_version_stays_with_the_setting_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when the track credits the guest, as here."""
    setting(monkeypatch, strip=False)
    qobuz = release(("F For You", "ft. Mary J Blige"))
    qobuz["tracks"]["1"]["1"]["artists"].append(("Mary J Blige", "guest"))

    titles = written(("Qobuz", qobuz))

    assert titles == ["F For You (ft. Mary J Blige)"]


def test_a_placeholder_taking_a_store_s_title_with_no_version_is_written_as_a_store_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setting(monkeypatch, strip=True)

    titles = written(("Beatport", release("Work (Original Mix)")), files=release("Track 01"))

    assert titles == ["Work"]


def test_as_before_a_store_s_featuring_version_naming_someone_the_track_doesn_t_credit_stays() -> None:
    """Qobuz with no performers line: the version is the only place the guest is named."""
    data = {
        "artist": {"name": "Disclosure"},
        "artists": [{"name": "Disclosure", "roles": ["main-artist"]}],
        "tracks": {"items": [{"track_number": 1, "title": "F For You", "version": "ft. Mary J Blige"}]},
    }
    qobuz = release("F For You")
    qobuz["tracks"] = anyio.run(Qobuz().parse_tracks, data)

    decide_alone(qobuz, "Qobuz")

    assert qobuz["tracks"]["1"]["1"]["title"] == "F For You (ft. Mary J Blige)"


def test_as_before_a_featuring_clause_naming_a_band_called_x_stays() -> None:
    titles = written(("Discogs", release("Song")), files=release("Song (feat. X)", replay_gain="-7.0 dB"))

    assert titles == ["Song (feat. X)"]


def test_accents_reach_a_files_title_that_folds_a_version_the_store_lacks() -> None:
    files = release("Ny Batteri (Remastered)", replay_gain="-7.0 dB")

    titles = written(("Discogs", release("Ný batterí")), files=files)

    assert titles == ["Ný Batterí (Remastered)"]


@pytest.mark.parametrize("placeholder", ["Track 01", "Audio Track 1", "Piste 01", "Pista 1", "Titel 03"])
def test_a_ripper_s_placeholder_in_several_languages_takes_the_store_s_title(placeholder: str) -> None:
    titles = written(("Deezer", release(("Song", None))), files=release(placeholder, replay_gain="-7.0 dB"))

    assert titles == ["Song"]


@pytest.mark.parametrize(
    ("first", "second", "third", "expected"),
    [
        (("Deezer", "Track 1"), ("Qobuz", ("Intro", "Live")), ("Discogs", "Intro (Live Version)"), "Intro (Live)"),
        (("Deezer", ("", "Remix")), ("Qobuz", ("Intro", None)), ("Discogs", "Other"), "Intro"),
    ],
)
def test_a_placeholder_takes_the_first_store_that_names_the_track(
    first: tuple, second: tuple, third: tuple, expected: str
) -> None:
    """A store that names nothing either ("Track 1", an empty title) is passed over."""
    stores = [(source, release(given)) for source, given in (first, second, third)]

    titles = written(*stores, files=release("Track 01", replay_gain="-7.0 dB"))

    assert titles == [expected]


def test_a_track_one_store_paired_is_known_to_the_next_store_by_that_store_s_title() -> None:
    """The files' "Ojeblikket" is the acoustic version, which Qobuz knows by its ISRC. Discogs' plain "Øjeblikket"
    then can't claim it by the files' title alone: the track is also "Øjeblikket (Acoustic)", as Qobuz named it."""
    files = release("Ojeblikket", "Øjeblikket (feat. Bob)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["isrc"], files["tracks"]["1"]["2"]["isrc"] = "ACOUSTIC", "PLAIN"
    qobuz = release(("Øjeblikket", None), ("Øjeblikket", "Acoustic"))
    qobuz["tracks"]["1"]["1"]["isrc"], qobuz["tracks"]["1"]["2"]["isrc"] = "PLAIN", "ACOUSTIC"
    discogs = release("Øjeblikket", "Øjeblikket (Acoustic)")
    discogs["tracks"]["1"]["1"]["artists"].append(("Pia", "guest"))
    discogs["tracks"]["1"]["2"]["artists"].append(("Dee", "guest"))

    merged = combine_metadatas(("Qobuz", qobuz), ("Discogs", discogs), base=files)

    guests = {t["isrc"]: [n for n, r in t["artists"] if r == "guest"] for t in merged["tracks"]["1"].values()}
    assert guests == {"ACOUSTIC": ["Dee"], "PLAIN": ["Pia"]}


def test_a_placeholder_named_by_one_store_pairs_the_next_store_by_that_name() -> None:
    files = release("Track 01", "Track 02", replay_gain="-7.0 dB")
    for track in files["tracks"]["1"].values():
        track["numbered"] = False
    discogs = release("Zebra (Mixed)", "Apple (Mixed)")
    discogs["tracks"]["1"]["1"]["isrc"], discogs["tracks"]["1"]["2"]["isrc"] = "ZEBRA", "APPLE"

    merged = combine_metadatas(
        ("Deezer", release("Track 1", "Track 2")),
        ("Qobuz", release(("Apple", "Mixed"), ("Zebra", "Mixed"))),
        ("Discogs", discogs),
        base=files,
    )

    assert {t["title"]: t["isrc"] for t in merged["tracks"]["1"].values()} == {
        "Apple (Mixed)": "APPLE",
        "Zebra (Mixed)": "ZEBRA",
    }


@pytest.mark.parametrize("role", ["producer", "composer", "remixer"])
def test_as_before_a_featuring_clause_naming_someone_credited_in_another_role_stays(role: str) -> None:
    """A producer's or composer's credit says nothing of a guest: the title is the only place they're one."""
    files = release("Song (feat. Pharrell)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"].append(("Pharrell", role))

    titles = written(("Discogs", release("Song")), files=files)

    assert titles == ["Song (feat. Pharrell)"]


def test_a_featuring_clause_naming_a_main_artist_of_the_track_goes() -> None:
    files = release("Song (feat. Bob)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"].append(("Bob", "main"))

    titles = written(("Discogs", release("Song")), files=files)

    assert titles == ["Song"]


def test_with_no_files_a_store_s_version_takes_the_next_store_s_accents() -> None:
    titles = written(
        ("Deezer", release(("Homework", "Tiesto Remix"))), ("Qobuz", release(("Homework", "Tiësto Remix")))
    )

    assert titles == ["Homework (Tiësto Remix)"]


def test_with_no_files_a_decomposed_title_s_version_takes_the_next_store_s_accents() -> None:
    """The first store's "Café" in decomposed form: the version's accents come back as well as the title's."""
    titles = written(("Deezer", release(("Cafe\u0301", "Tiesto Remix"))), ("Qobuz", release(("Café", "Tiësto Remix"))))

    assert titles == ["Café (Tiësto Remix)"]


def test_the_retagger_matches_a_file_to_its_title_written_without_a_credited_featuring_clause() -> None:
    """Unnumbered files with no ISRC: "Alive (feat. Bob)" is the track the merge wrote as "Alive"."""
    items = [("01.flac", {"title": "Alive (feat. Bob)"}), ("02.flac", {"title": "Song 2 (feat. Zed)"})]
    tracks = [
        {"title": title, "isrc": None, "duration": None, "track#": n} for n, title in (("1", "Song 2"), ("2", "Alive"))
    ]

    ordered, _reasons = order_tracks_for_files(items, tracks, show=False)

    assert [track["title"] for track in ordered] == ["Alive", "Song 2"]


def test_the_retagger_tells_apart_two_files_the_merge_wrote_as_one_title_by_whom_each_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unnumbered files of one length with no ISRC, both written "Song": each file is the track crediting its guest."""
    setting(monkeypatch, strip=True)
    items = [("01.flac", {"title": "Song (feat. Bob)"}), ("02.flac", {"title": "Song (feat. Alice)"})]
    tracks = [
        {"title": "Song", "artists": [("Main", "main"), (guest, "guest")], "isrc": None, "duration": 200, "track#": n}
        for n, guest in (("1", "Alice"), ("2", "Bob"))
    ]

    ordered, _reasons = order_tracks_for_files(items, tracks, show=False)

    assert [track["artists"][1][0] for track in ordered] == ["Bob", "Alice"]


@pytest.mark.parametrize(
    ("numbered", "files"),
    [(False, ["Song [feat. Bob]", "Song (feat. Bob)"]), (True, ["Song (feat. Bob)", "Song (feat. Bob & Zed)"])],
)
def test_the_retagger_gives_a_file_to_the_track_titled_as_it_before_one_its_title_is_written_as(
    monkeypatch: pytest.MonkeyPatch, numbered: bool, files: list[str]
) -> None:
    """The first file is the track whose store credits no guest, so its title keeps the clause; the second is the
    track crediting Bob (and Zed), written "Song". Bob is credited on that track, but the first file is the other's."""
    setting(monkeypatch, strip=True)
    items = [("01.flac", {"title": files[0]}), ("02.flac", {"title": files[1]})]
    tracks = [
        {
            "title": "Song",
            "artists": [("Main", "main"), ("Bob", "guest"), ("Zed", "guest")],
            "isrc": None,
            "track#": "1",
        },
        {"title": files[0], "artists": [("Main", "main")], "isrc": None, "track#": "2"},
    ]

    ordered, _reasons = order_tracks_for_files(items, tracks, numbered=numbered, show=False)

    assert [track["track#"] for track in ordered] == ["2", "1"]


# Each title's reason: which text it is, then what changed it.


def titled(
    title: str | None, version: str | None = None, stores: Sequence[tuple[str | None, str | None]] = (), **marks: object
) -> dict:
    return {"title": title, "version": version, "store_titles": list(stores), **marks}


@pytest.mark.parametrize(
    ("track", "files", "credited", "expected"),
    [
        (titled("Song", stores=[("Song", None)]), True, set(), ("Song", "the files' title")),
        (
            titled("Ny Batteri", stores=[("Ný batterí", None)]),
            True,
            set(),
            ("Ný Batterí", "the files' title, with a store's accents"),
        ),
        (
            titled("Song (feat. Bob)", stores=[("Song", None)]),
            True,
            {"bob"},
            ("Song", "the files' title, without a credited featuring clause"),
        ),
        (
            titled("Intro", "Live", placeholder_from="Qobuz"),
            True,
            set(),
            ("Intro (Live)", "Qobuz's title, for a placeholder"),
        ),
        (titled("Song", "Live"), False, set(), ("Song (Live)", "the store's title and version")),
        (titled("Intro", placeholder_from="Qobuz"), False, set(), ("Intro", "Qobuz's title, for a placeholder")),
        (
            titled("Song", "feat. Bob"),
            False,
            {"bob"},
            ("Song", "the store's title and version, without a credited featuring clause"),
        ),
        (titled(None), True, set(), (None, "no title")),
        (titled("", "Remix"), False, set(), ("", "no title")),
        # A store's "ft." clause goes as the files' does, though the setting's own pattern drops only "feat.".
        (
            titled("Song (ft. Bob)", "Live"),
            False,
            {"bob"},
            ("Song (Live)", "the store's title and version, without a credited featuring clause"),
        ),
        # The accents were only in the clause that went: nothing written came from the store.
        (
            titled("Song (feat. Bob)", stores=[("Song (feat. Bób)", None)]),
            True,
            {"bob"},
            ("Song", "the files' title, without a credited featuring clause"),
        ),
    ],
)
def test_each_title_gives_the_rule_that_wrote_it_and_what_changed_it(
    monkeypatch: pytest.MonkeyPatch, track: dict, files: bool, credited: set[str], expected: tuple
) -> None:
    setting(monkeypatch, strip=True)

    written = _written_title(track, files, credited)

    assert written == expected


def test_the_merge_marks_the_store_that_named_a_placeholder() -> None:
    """The first store that names it: Deezer's "Track 1" names nothing either."""
    base = release("Track 01", replay_gain="-7.0 dB")
    combine_tracks(base["tracks"], release("Track 1")["tracks"], True, sources=("files", "Deezer"))
    combine_tracks(base["tracks"], release(("Intro", "Live"))["tracks"], True, sources=("files", "Qobuz"))

    written = _written_title(base["tracks"]["1"]["1"], True, set())

    assert written == ("Intro (Live)", "Qobuz's title, for a placeholder")


# What holds for every title, over many generated ones.

_ACCENTED = ["Ný batterí", "Łódź", "Øjeblikket", "Café", "Ágæt", "Söng", "Intro", "Live and Let Die", "Born to Run"]
_VERSIONS = [None, "Live", "Remastered", "Original Mix", "Tiësto Remix", "Demo", "Mixed", "Live à Paris"]


_STROKED = {"ł": "l", "Ł": "L", "ø": "o", "Ø": "O"}  # letters that decompose to no plain one


def _plain(text: str) -> str:
    """The text without its accents, letter by letter: what a hand-tagged rip may hold."""
    return "".join(_STROKED.get(letter) or unicodedata.normalize("NFD", letter)[0] for letter in text)


def _generated_titles(count: int):
    """(files title, the store's (title, version), the store's text accented as the files' would be)."""
    rng = random.Random(1162)
    for _ in range(count):
        word, version = rng.choice(_ACCENTED), rng.choice(_VERSIONS)
        accented = f"{word} ({version})" if version else word
        files = "".join(
            plain if rng.random() < 0.6 else letter for letter, plain in zip(accented, _plain(accented), strict=True)
        )
        files = rng.choice([files, files.upper(), files.title(), unicodedata.normalize("NFD", files)])
        folded = rng.random() < 0.5
        yield files, ((accented, None) if folded else (word, version)), accented


@pytest.mark.parametrize("strip", [True, False])
def test_a_store_restores_its_accents_to_the_files_title_in_the_files_case(
    monkeypatch: pytest.MonkeyPatch, strip: bool
) -> None:
    """The files' text is kept but for the letters a store accents: no store's version is added to it."""
    setting(monkeypatch, strip=strip)
    for files, store, accented in _generated_titles(1500):
        expected = "".join(
            a.upper() if f.isupper() else a.lower() if f.islower() else a
            for a, f in zip(accented, unicodedata.normalize("NFC", files), strict=True)
        )

        written, why = _written_title(titled(files, stores=[store]), True, set())

        assert unicodedata.normalize("NFC", written) == expected, (files, store, written)
        assert ("accents" in why) == (written != files), (files, store, written, why)


@pytest.mark.parametrize("strip", [True, False])
def test_a_store_s_version_is_in_its_written_title_unless_the_setting_drops_it(
    monkeypatch: pytest.MonkeyPatch, strip: bool
) -> None:
    setting(monkeypatch, strip=strip)
    rng = random.Random(1162)
    for _ in range(1500):
        title, version = rng.choice(_ACCENTED), rng.choice([*_VERSIONS, "feat. Bob"])
        credited = rng.choice([set(), {"bob"}])
        # The setting's words among _VERSIONS, and a credited guest's version.
        dropped = strip and (version in {"Remastered", "Original Mix"} or (version == "feat. Bob" and bool(credited)))

        written, _why = _written_title(titled(title, version), False, credited)

        said = written[len(title) :].lower()  # its own words aside: "Live" in "Live and Let Die" is not the version
        assert written.startswith(title), (title, version, written)
        if version:
            assert (version.lower() in said) != dropped, (title, version, written)


@pytest.mark.parametrize("strip", [True, False])
def test_a_featuring_clause_goes_exactly_when_the_setting_is_on_and_each_name_is_credited(
    monkeypatch: pytest.MonkeyPatch, strip: bool
) -> None:
    setting(monkeypatch, strip=strip)
    rng = random.Random(1162)
    clauses = {
        "Bob": {"bob"},
        "Alice & X": {"alice", "x"},
        "Bob x Carol": {"bob", "carol"},
        "Tyler, The Creator": {"tylerthecreator"},
    }
    for _ in range(1500):
        names, needed = rng.choice(list(clauses.items()))
        # Some, all or none of the clause's names credited, and maybe someone else.
        credited = set(rng.sample(sorted(needed | {"dave"}), k=rng.randint(0, len(needed) + 1)))
        files = rng.random() < 0.5
        in_version = not files and rng.random() < 0.5
        track = titled("Song", f"feat. {names}") if in_version else titled(f"Song (feat. {names})")
        goes = strip and needed <= credited

        written, why = _written_title(track, files, credited)

        assert (names not in written) == goes, (track, files, credited, written)
        assert ("featuring" in why) == goes, (track, files, credited, why)


@pytest.mark.parametrize("strip", [True, False])
def test_a_title_decided_again_is_unchanged(monkeypatch: pytest.MonkeyPatch, strip: bool) -> None:
    """No version is added twice: a written title, given its version again, stays as it is."""
    setting(monkeypatch, strip=strip)
    rng = random.Random(1162)
    for _ in range(1500):
        title, version = rng.choice(_ACCENTED), rng.choice(_VERSIONS)
        once, _why = _written_title(titled(title, version), False, set())
        again, _why = _written_title(titled(once, version), False, set())

        assert again == once, (title, version, once)


def _as_files_hold_it(word: str, version: str | None, rng: random.Random) -> str:
    """The store's title folded as the store shows it, or as salmon writes it."""
    return rng.choice([f"{word} ({version})" if version else word, title_with_version(word, version)])


@pytest.mark.parametrize("strip", [True, False])
@pytest.mark.parametrize("numbered", [True, False])
def test_each_file_pairs_with_its_own_track_whichever_form_of_the_title_it_holds(
    monkeypatch: pytest.MonkeyPatch, strip: bool, numbered: bool
) -> None:
    """Files folding a store's version, or holding its title as written, in another order: distinct titles pair. Each
    file carries its store's word, to check whom it paired with."""
    setting(monkeypatch, strip=strip)
    rng = random.Random(1162)
    words = ["Apple", "Zebra", "Mango", "Kiwi", "Plum", "Pear"]
    for _ in range(500):
        stores = [{"title": word, "version": rng.choice(_VERSIONS), "isrc": None, "duration": None} for word in words]
        files = [
            {"title": _as_files_hold_it(t["title"], t["version"], rng), "isrc": None, "duration": None}
            | {"numbered": numbered, "word": t["title"]}
            for t in stores
        ]
        if numbered:
            i, j = rng.sample(range(len(files)), 2)
            files[i], files[j] = files[j], files[i]
        else:
            rng.shuffle(files)

        paired, _reasons = _pair_base_tracks(files, stores)

        assert [f and f["word"] for f in paired] == words, (files, stores)
