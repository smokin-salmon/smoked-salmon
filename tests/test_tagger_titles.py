"""A track's title and its version (#869 step 3b, #1077): kept apart by the stores that give them apart, and written
once, after the last store is merged, from the files' title and what each store gave beside it."""

from copy import deepcopy

import anyio
import msgspec
import pytest
from test_tagger_combine import make_metadata

from salmon import cfg
from salmon.tagger.combine import combine_metadatas, decide_alone
from salmon.tagger.sources.base import MetadataMixin
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

    assert Deezer().determine_rls_type(meta)[1] == "Remix"


def test_as_before_original_mixes_given_apart_do_not_make_a_remix_release() -> None:
    meta = release(*((f"Song {n}", "Original Mix") for n in range(1, 9)))

    assert Deezer().determine_rls_type(meta)[1] != "Remix"


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
    merged = combine_metadatas(("Qobuz", release(("Song", "X Remix"))), ("Discogs", release("Song")))
    alone = release(("Song", "X Remix"))
    decide_alone(alone, "Deezer")

    assert not {"version", "store_titles"} & merged["tracks"]["1"]["1"].keys()
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

    assert written((source, release("Song (feat. Xavier)"))) == ["Song (feat. Xavier)"]


def test_with_no_files_a_store_s_featuring_clause_naming_only_credited_guests_goes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As with the files: the credit says it already."""
    setting(monkeypatch, strip=True)
    discogs = release("Song (feat. Xavier)")
    discogs["tracks"]["1"]["1"]["artists"].append(("Xavier", "guest"))

    assert written(("Discogs", discogs)) == ["Song"]


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

    assert written(("Deezer", release("Pre")), files=files) == ["Pre"]


def test_as_before_the_files_featuring_clause_stays_with_the_setting_off(monkeypatch: pytest.MonkeyPatch) -> None:
    setting(monkeypatch, strip=False)
    files = release("Pre (feat. Alice)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"].append(("Alice", "guest"))

    assert written(("Deezer", release("Pre")), files=files) == ["Pre (feat. Alice)"]


def test_a_featuring_clause_naming_an_artist_the_track_doesn_t_credit_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    """It isn't redundant then: the title is the only place the guest is named."""
    setting(monkeypatch, strip=True)

    assert written(("Discogs", release("Hard to Love")), files=release("Hard to Love (feat. Jessie Reyez)")) == [
        "Hard to Love (feat. Jessie Reyez)"
    ]


def test_a_files_track_with_no_title_takes_the_first_store_s() -> None:
    files = release("x")
    files["tracks"]["1"]["1"]["title"] = None

    assert written(("Qobuz", release(("Song", "Alice Remix"))), files=files) == ["Song (Alice Remix)"]


def test_accents_come_from_a_store_s_title_that_folds_its_version() -> None:
    assert written(("Discogs", release("Ný batterí (Remastered)")), files=release("Ny Batteri")) == ["Ný Batterí"]


def test_a_featuring_clause_naming_a_credited_artist_with_a_comma_in_their_name_goes() -> None:
    files = release("Sasquatch (feat. Tyler, The Creator)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"].append(("Tyler, The Creator", "guest"))

    assert written(("Discogs", release("Sasquatch")), files=files) == ["Sasquatch"]


def test_a_featuring_clause_joining_credited_artists_with_and_goes() -> None:
    files = release("Song (feat. Alice and Bob)", replay_gain="-7.0 dB")
    files["tracks"]["1"]["1"]["artists"] += [("Alice", "guest"), ("Bob", "guest")]

    assert written(("Discogs", release("Song")), files=files) == ["Song"]


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

    assert written(("Qobuz", qobuz)) == ["F For You (ft. Mary J Blige)"]


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
    assert written(("Discogs", release("Song")), files=release("Song (feat. X)", replay_gain="-7.0 dB")) == [
        "Song (feat. X)"
    ]


def test_accents_reach_a_files_title_that_folds_a_version_the_store_lacks() -> None:
    files = release("Ny Batteri (Remastered)", replay_gain="-7.0 dB")

    assert written(("Discogs", release("Ný batterí")), files=files) == ["Ný Batterí (Remastered)"]


@pytest.mark.parametrize("placeholder", ["Track 01", "Audio Track 1", "Piste 01", "Pista 1", "Titel 03"])
def test_a_ripper_s_placeholder_in_several_languages_takes_the_store_s_title(placeholder: str) -> None:
    assert written(("Deezer", release(("Song", None))), files=release(placeholder, replay_gain="-7.0 dB")) == ["Song"]
