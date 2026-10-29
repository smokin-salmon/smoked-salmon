import asyncio
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salmon.errors import TrackCombineError
from salmon.tagger.combine import (
    _extract_remixers_from_title,
    _refuse_renumbering_that_loses_a_track,
    check_for_artist_fragments,
    combine_metadatas,
    combine_tracks,
)
from salmon.tagger.pre_data import parse_artists
from salmon.tagger.retagger import create_artist_str
from salmon.tagger.sources.base import append_remixers_to_track_titles
from salmon.tagger.sources.discogs import Scraper, parse_credits
from salmon.tagger.sources.discogs import parse_artists as discogs_parse_artists


def make_metadata(title: str) -> dict:
    return {
        "artists": [("Marius Acke", "main")],
        "title": title,
        "group_year": "2026",
        "year": "2026",
        "date": None,
        "edition_title": None,
        "label": None,
        "catno": None,
        "rls_type": None,
        "genres": ["Electronic"],
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
        "source": "WEB",
        "cover": None,
        "upc": None,
        "comment": None,
        "urls": [],
        "tracks": {
            "1": {
                "1": {
                    "track#": "1",
                    "disc#": "1",
                    "tracktotal": "1",
                    "disctotal": "1",
                    "artists": [("Marius Acke", "main")],
                    "title": "Good Funky BD",
                    "replay_gain": None,
                    "peak": None,
                    "isrc": None,
                    "explicit": None,
                    "format": None,
                    "streamable": None,
                }
            }
        },
    }


def make_tracks(discs: dict[str, list[str]], *, isrc_prefix: str | None) -> dict:
    return {
        disc_no: {
            track_no: {
                "track#": track_no,
                "disc#": disc_no,
                "tracktotal": str(len(track_nos)),
                "disctotal": str(len(discs)),
                "artists": [("Marius Acke", "main")],
                "title": f"Track {disc_no}-{track_no}",
                "replay_gain": None,
                "peak": None,
                "isrc": f"{isrc_prefix} {disc_no}-{track_no}" if isrc_prefix is not None else None,
                "explicit": None,
                "format": None,
                "streamable": None,
            }
            for track_no in track_nos
        }
        for disc_no, track_nos in discs.items()
    }


def test_preferred_source_title_overrides_stale_local_album_title():
    base = make_metadata("TOW014 / Marius Acke - Dirty & Funky EP")
    preferred = make_metadata("Dirty & Funky EP")
    preferred["genres"] = ["Deep House"]
    preferred["url"] = "https://mariusacke.bandcamp.com/album/dirty-funky-ep"

    combined = combine_metadatas(
        ("Bandcamp", preferred),
        base=deepcopy(base),
        source_url=preferred["url"],
    )

    assert combined["title"] == "Dirty & Funky EP"


def test_combine_tracks_keeps_all_tracks_when_updating_track_numbers():
    ordered_tracks = [str(i) for i in range(1, 12)]
    base = make_tracks({"1": ["1", "10", "11", "2", "3", "4", "5", "6", "7", "8", "9"]}, isrc_prefix=None)
    meta = make_tracks({"1": ordered_tracks}, isrc_prefix="ISRC")

    combined = combine_tracks(deepcopy(base), deepcopy(meta), True)

    assert list(combined["1"].keys()) == ordered_tracks
    assert len(combined["1"]) == 11
    assert [combined["1"][str(i)]["isrc"] for i in range(1, 12)] == [f"ISRC 1-{i}" for i in range(1, 12)]


def test_combine_tracks_sorts_multi_disc_provider_tracks_and_discs():
    base = make_tracks({"2": ["1", "2", "10"], "1": ["1", "2", "10"]}, isrc_prefix=None)
    meta = make_tracks({"2": ["1", "10", "2"], "1": ["1", "10", "2"]}, isrc_prefix="ISRC")

    combined = combine_tracks(deepcopy(base), deepcopy(meta), True)

    assert list(combined["1"].keys()) == ["1", "2", "10"]
    assert list(combined["2"].keys()) == ["1", "2", "10"]
    assert combined["1"]["1"]["isrc"] == "ISRC 1-1"
    assert combined["1"]["2"]["isrc"] == "ISRC 1-2"
    assert combined["1"]["10"]["isrc"] == "ISRC 1-10"
    assert combined["2"]["1"]["isrc"] == "ISRC 2-1"
    assert combined["2"]["2"]["isrc"] == "ISRC 2-2"
    assert combined["2"]["10"]["isrc"] == "ISRC 2-10"


def one_track(artists: list[tuple[str, str]], title: str) -> dict:
    track = make_tracks({"1": ["1"]}, isrc_prefix=None)["1"]["1"]
    return {"1": {"1": {**track, "artists": artists, "title": title}}}


def test_combine_tracks_feat_credit_from_tag_and_discogs_gives_one_guest_list():
    # Discogs release 2520452, track 4-11, tag "Shawfire feat. Ultra (27) & Tullisa"
    base = one_track(parse_artists("Shawfire feat. Ultra (27) & Tullisa"), "Work It Out")
    discogs_track = {
        "artists": [
            {"name": "Shawfire", "join": "Feat."},
            {"name": "Ultra (27)", "join": "&"},
            {"name": "Tullisa", "join": ""},
        ]
    }
    meta = one_track(discogs_parse_artists([], discogs_track), "Work It Out")

    combined = combine_tracks(deepcopy(base), deepcopy(meta), True)

    assert sorted(combined["1"]["1"]["artists"]) == [("Shawfire", "main"), ("Tullisa", "guest"), ("Ultra", "guest")]


DISCOGS_1_16 = {
    "artists": [{"name": "Selena (5)", "join": "Vs."}, {"name": "X.Men", "anv": "X-Men", "join": ""}],
    "extraartists": [
        {"name": "DJ Face", "role": "Remix"},
        {"name": 'Matt "Jam" Lamont', "role": "Remix"},
    ],
}


def test_combine_tracks_does_not_duplicate_artists_spelled_with_different_punctuation():
    # Discogs release 2520452, track 1-16: Discogs names it "X.Men" (anv "X-Men")
    base = one_track([("Selena", "main"), ("X-Men", "main")], "Give It Up (Jamm & Face Remix)")
    meta = one_track(discogs_parse_artists([], DISCOGS_1_16), "Give It Up (Jamm & Face Remix)")

    combined = combine_tracks(deepcopy(base), deepcopy(meta), True)

    assert combined["1"]["1"]["artists"] == [
        ("Selena", "main"),
        ("X-Men", "main"),
        ("DJ Face", "remixer"),
        ('Matt "Jam" Lamont', "remixer"),
    ]


def test_combine_tracks_issue_tag_with_vs_credit_gives_no_duplicate_or_suffix():
    # The tag of track 1-16 is one string, "Selena (5) vs. X-Men", so it is one name to salmon
    base = one_track(parse_artists("Selena (5) vs. X-Men"), "Give It Up (Jamm & Face Remix)")
    meta = one_track(discogs_parse_artists([], DISCOGS_1_16), "Give It Up (Jamm & Face Remix)")

    combined = combine_tracks(deepcopy(base), deepcopy(meta), True)

    assert combined["1"]["1"]["artists"] == [
        ("Selena vs. X-Men", "main"),
        ("DJ Face", "remixer"),
        ('Matt "Jam" Lamont', "remixer"),
    ]


def test_fragment_check_keeps_names_that_only_share_letters():
    for artists in (["Eve", "Steve Aoki"], ["Ann", "Joanna"], ["Mos", "Kosmos"], ["Rae", "Raekwon"], ["A B", "ABBA"]):
        pairs = [(name, "main") for name in artists]

        kept = check_for_artist_fragments(list(pairs))

        assert kept == pairs, artists


def test_fragment_check_drops_a_name_that_is_whole_words_of_another():
    artists = [("Ultra", "main"), ("Ultra Nate", "main"), ("Nate", "guest")]

    kept = check_for_artist_fragments(artists)

    assert kept == [("Ultra Nate", "main")]


def test_fragment_check_matches_words_ignoring_case_and_punctuation():
    artists = [("X.Men", "main"), ("Selena vs. X-Men", "main"), ("selena", "main")]

    kept = check_for_artist_fragments(artists)

    assert kept == [("Selena vs. X-Men", "main")]


def test_fragment_check_keeps_single_letter_names():
    artists = [("A", "main"), ("A Tribe Called Quest", "main")]

    kept = check_for_artist_fragments(list(artists))

    assert kept == artists


def test_combine_tracks_still_requires_the_same_role_to_match():
    base = one_track([("X-Men", "main")], "Song")
    meta = one_track([("X.Men", "guest")], "Song")

    combined = combine_tracks(deepcopy(base), deepcopy(meta), True)

    assert combined["1"]["1"]["artists"] == [("X-Men", "main"), ("X.Men", "guest")]


def test_extract_remixers_does_not_span_an_earlier_paren_group():
    # Regression for #430: the old .*? was greedy across parens, pulling in the
    # preceding group and half its parentheses as part of the remixer name.
    title = "Still (I Got Summer On My Mind) (FORTELLA Remix)"

    remixers = _extract_remixers_from_title(title)

    assert remixers == [("FORTELLA", "remixer")]


def test_extract_remixers_plain_title():
    title = "Title (X Remix)"

    remixers = _extract_remixers_from_title(title)

    assert remixers == [("X", "remixer")]


def test_extract_remixers_no_remix_in_title():
    title = "Title With No Remix Marker"

    remixers = _extract_remixers_from_title(title)

    assert remixers == []


# Discogs release 2520452, trimmed to the credits the merge reads
DISCOGS_3_3 = {
    "artists": [{"name": "K-Ci & JoJo", "anv": "K-Ci And JoJo", "join": ""}],
    "extraartists": [{"name": "Club Asylum", "anv": "", "role": "Remix"}],
}
DISCOGS_3_12 = {
    "artists": [{"name": "Shanks & Bigfoot", "anv": "Shanks And Bigfoot", "join": ""}],
    "extraartists": [{"name": "Metro (3)", "anv": "", "role": "Remix"}],
}
DISCOGS_4_6 = {
    "artists": [
        {"name": "Gomi", "anv": "DJ Gomi", "join": "Feat."},
        {"name": 'Louie "Balo" Guzman', "anv": "Loui Balo", "join": "&"},
        {"name": "Yasmeen", "anv": "", "join": ""},
    ],
    "extraartists": [{"name": "Trouble Men", "anv": "", "role": "Remix"}],
}


def discogs_track(discogs: dict, title: str) -> dict:
    artists, variations = parse_credits([], discogs)
    meta = one_track(artists, title)
    meta["1"]["1"]["artist_variations"] = variations
    return meta


def merge(tag_artists: list[tuple[str, str]], discogs: dict) -> list[tuple[str, str]]:
    base = one_track(tag_artists, "Title")
    meta = discogs_track(discogs, "Title")
    return combine_tracks(deepcopy(base), deepcopy(meta), True)["1"]["1"]["artists"]


def test_combine_tracks_keeps_the_tag_spelling_matching_the_anv_k_ci_and_jojo():
    # Fails on master: proposes "K-Ci and JoJo & K-Ci & JoJo"
    artists = merge(parse_artists("K-Ci and JoJo"), DISCOGS_3_3)

    assert [a for a in artists if a[1] == "main"] == [("K-Ci and JoJo", "main")]
    assert create_artist_str(artists) == "K-Ci and JoJo"
    assert ("Club Asylum", "remixer") in artists


def test_combine_tracks_keeps_the_tag_spelling_matching_the_anv_shanks_and_bigfoot():
    # Fails on master: proposes "Shanks and Bigfoot & Shanks & Bigfoot"
    artists = merge(parse_artists("Shanks and Bigfoot"), DISCOGS_3_12)

    assert [a for a in artists if a[1] == "main"] == [("Shanks and Bigfoot", "main")]
    assert create_artist_str(artists) == "Shanks and Bigfoot"


def test_combine_tracks_matches_feat_credit_tags_on_the_anv():
    # Fails on master: proposes "DJ Gomi (feat. Loui Balo, Yasmeen, Louie "Balo" Guzman)"
    artists = merge(parse_artists("DJ Gomi feat. Loui Balo & Yasmeen"), DISCOGS_4_6)

    assert create_artist_str([a for a in artists if a[1] != "remixer"]) == "DJ Gomi (feat. Loui Balo & Yasmeen)"


def test_combine_tracks_proposes_the_main_discogs_name_not_the_anv_when_no_tag_matches():
    # Passes on master too: the ANV is only ever used to match, never proposed
    artists = merge(parse_artists("Somebody Else"), DISCOGS_3_3)

    assert ("K-Ci & JoJo", "main") in artists
    assert ("K-Ci And JoJo", "main") not in artists


def test_combine_tracks_without_tag_artists_proposes_the_main_discogs_name():
    artists = merge([], DISCOGS_3_3)

    assert ("K-Ci & JoJo", "main") in artists
    assert ("K-Ci And JoJo", "main") not in artists


def test_combine_tracks_empty_anv_never_matches_an_empty_tag_name():
    discogs = {"artists": [{"name": "Yasmeen", "anv": "", "join": ""}]}

    artists = merge([("", "main")], discogs)

    assert ("Yasmeen", "main") in artists


def test_combine_tracks_anv_match_still_requires_the_same_role():
    artists = merge([("K-Ci And JoJo", "guest")], DISCOGS_3_3)

    assert ("K-Ci & JoJo", "main") in artists


def test_combine_metadatas_leaves_the_anv_out_of_the_final_metadata():
    base = make_metadata("Album")
    base["tracks"] = {
        "1": {"1": {**make_metadata("Album")["tracks"]["1"]["1"], "artists": parse_artists("K-Ci and JoJo")}}
    }
    discogs = make_metadata("Album")
    artists, variations = parse_credits([], DISCOGS_3_3)
    discogs["tracks"]["1"]["1"] = {
        **discogs["tracks"]["1"]["1"],
        "artists": artists,
        "artist_variations": variations,
    }

    combined = combine_metadatas(("Discogs", discogs), base=deepcopy(base))

    assert "artist_variations" not in combined["tracks"]["1"]["1"]
    assert ("K-Ci and JoJo", "main") in combined["artists"]
    assert ("K-Ci & JoJo", "main") not in combined["artists"]


def test_combine_metadatas_from_discogs_alone_leaves_the_anv_out_too():
    discogs = make_metadata("Album")
    artists, variations = parse_credits([], DISCOGS_3_3)
    discogs["tracks"]["1"]["1"] = {
        **discogs["tracks"]["1"]["1"],
        "artists": artists,
        "artist_variations": variations,
    }

    combined = combine_metadatas(("Discogs", discogs))

    assert "artist_variations" not in combined["tracks"]["1"]["1"]


def remix_title(title: str, remixers: tuple[str, ...] = ("X",)) -> str:
    data = {"1": {"1": {"title": title, "artists": [("Lead", "main"), *((r, "remixer") for r in remixers)]}}}

    return append_remixers_to_track_titles(data)["1"]["1"]["title"]


@pytest.mark.parametrize(
    "title",
    [
        "Nobody Else (London Connection Edit)",
        "Walking Zero (Tuff & Jam Unda-Vybe Vocal)",
        "Too Close (London Connection 2 Step Dub)",
        "R U Sleeping (Blunt Edit)",
        "What Would We Do? (Clay and Deller's Industry Standard Edit)",
        "Song [Club Dub]",
        "Song (Feather Edit)",
        "Song (Edit) ",
    ],
)
def test_remixer_is_not_appended_to_a_title_that_ends_in_a_version(title):
    assert remix_title(title) == title


def test_remixers_are_separated_as_the_artists_are():
    # Remixers were always joined with " & ", so "Song (A & B & C Remix)".
    assert remix_title("Song", ("A", "B", "C")) == "Song (A, B, C Remix)"


def test_remixed_is_not_appended_to_a_title_that_ends_in_a_version():
    assert remix_title("Song (Dub)", ("A", "B", "C", "D")) == "Song (Dub)"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Song", "Song (X Remix)"),
        ("Song (feat. Y)", "Song (feat. Y) (X Remix)"),
        ("Song [Feat Y]", "Song [Feat Y] (X Remix)"),
        ("Song (ft. Y)", "Song (ft. Y) (X Remix)"),
        ("Song (Featuring Y)", "Song (Featuring Y) (X Remix)"),
        ("Song (Edit) Part 2", "Song (Edit) Part 2 (X Remix)"),
        ("Song (X Remix)", "Song (X Remix)"),
        ("Song (X Mix)", "Song (X Mix)"),
    ],
)
def test_remixer_is_still_appended_unless_the_title_names_its_version(title, expected):
    assert remix_title(title) == expected


@pytest.mark.parametrize("group_year", [None, "2018"])
def test_dateless_source_preserves_tag_years(group_year):
    base = make_metadata("Release")
    base.update(year="2020", group_year=group_year)
    source = make_metadata("Release")
    source.update(year=None, group_year=None, date=None)

    combined = combine_metadatas(("MusicBrainz", source), base=base)

    assert combined["year"] == "2020"
    assert combined["group_year"] == (group_year or "2020")


def test_dated_source_can_still_supply_release_year():
    base = make_metadata("Release")
    base.update(year="2020", group_year=None)
    source = make_metadata("Release")
    source.update(year="2021", group_year="2019", date="2021-02-03")

    combined = combine_metadatas(("MusicBrainz", source), base=base)

    assert combined["year"] == "2021"
    assert combined["group_year"] == "2019"
    assert combined["date"] == "2021-02-03"


def local_files(*nums: str, numbered: bool = True, disc: str = "1") -> dict:
    """Local tracks as `create_track_list` gives them, without totals."""
    tracks = {}
    for n in nums:
        local = one_track([("A", "main")], f"Local {n}")["1"]["1"]
        tracks[n] = local | {"track#": n, "disc#": disc, "tracktotal": None, "disctotal": None, "numbered": numbered}
    return {disc: tracks}


def source_track(num: str, disc: str, tracktotal: int, disctotal: int) -> dict:
    track = one_track([("A", "main")], f"Source {num}")["1"]["1"]
    return track | {"track#": num, "disc#": disc, "tracktotal": str(tracktotal), "disctotal": str(disctotal)}


def test_discogs_vinyl_sides_keep_the_files_numbers_and_give_no_totals():
    # Discogs release 74364 (#765): heading, S, heading, 5 parse as disc 1 track S and disc 2 track 5.
    soup = {
        "artists": [{"name": "Adam F"}],
        "tracklist": [
            {"type_": "heading", "position": "", "title": "Other Side"},
            {"type_": "track", "position": "S", "title": "Burning Deep"},
            {"type_": "heading", "position": "", "title": "This Side"},
            {"type_": "track", "position": "5", "title": "Lighter Style"},
        ],
    }
    meta = asyncio.run(Scraper().parse_tracks(soup))
    assert set(meta) == {"1", "2"}

    combined = combine_tracks(local_files("01", "02"), meta, True)

    assert [t["track#"] for t in combined["1"].values()] == ["01", "02"]
    assert all(t["tracktotal"] is None and t["disctotal"] is None for t in combined["1"].values())


def test_single_disc_source_numbered_1_5_keeps_the_files_numbers():
    meta = {"1": {"1": source_track("1", "1", 2, 1), "5": source_track("5", "1", 2, 1)}}

    combined = combine_tracks(local_files("1", "2"), meta, True)

    assert sorted(combined["1"]) == ["1", "2"]
    assert all(t["tracktotal"] is None for t in combined["1"].values())


def test_one_bad_disc_makes_the_whole_source_unusable():
    base = {**local_files("1", "2", disc="1"), **local_files("3", "4", disc="2")}
    meta = {
        "1": {"1": source_track("1", "1", 2, 2), "2": source_track("2", "1", 2, 2)},
        "2": {"1": source_track("1", "2", 2, 2), "3": source_track("3", "2", 2, 2)},
    }

    combined = combine_tracks(base, meta, True)

    assert sorted(combined["1"]) == ["1", "2"]
    assert sorted(combined["2"]) == ["3", "4"]
    assert all(t["tracktotal"] is None for disc in combined.values() for t in disc.values())


def test_a_source_numbered_1_to_n_is_taken_as_before():
    # Regression guard: passes on master too, the usable path is unchanged.
    meta = {"1": {"1": source_track("1", "1", 2, 1), "2": source_track("2", "1", 2, 1)}}

    combined = combine_tracks(local_files("2", "3"), meta, True)

    assert sorted(combined["1"]) == ["1", "2"]
    assert all(t["tracktotal"] == "2" and t["disctotal"] == "1" for t in combined["1"].values())


def test_unnumbered_files_get_1_to_n_in_the_sources_order_when_the_source_is_unusable():
    meta = {
        "1": {
            "A1": source_track("A1", "1", 3, 1),
            "A2": source_track("A2", "1", 3, 1),
            "B1": source_track("B1", "1", 3, 1),
        }
    }

    combined = combine_tracks(local_files("7", "8", "9", numbered=False), meta, True)

    assert list(combined["1"]) == ["1", "2", "3"]
    assert all(t["tracktotal"] is None for t in combined["1"].values())


def test_refusing_a_lost_track_accepts_the_unusable_reported_shape():
    # An unpaired local track numbered 5 must not collide with the source's own "5" (disc 1).
    base = local_files("01", "02", "5")
    ordered = [base["1"]["01"], base["1"]["02"]]
    mtracks = [source_track("S", "1", 1, 2), source_track("5", "1", 1, 2)]

    _refuse_renumbering_that_loses_a_track(list(base["1"].values()), mtracks, ordered, False, True)  # raises nothing


def test_refusing_a_lost_track_treats_01_and_1_as_one_slot():
    # An unpaired file tagged "01" and an unnumbered file renumbered to 1 on the same disc would both be track 1.
    kept = local_files("01")["1"]["01"]
    moved = local_files("7", numbered=False)["1"]["7"]
    mtracks = [source_track("S", "1", 1, 1)]

    with pytest.raises(TrackCombineError, match="would hold two local tracks"):
        _refuse_renumbering_that_loses_a_track([kept, moved], mtracks, [moved], False, True)


def test_refusing_a_lost_track_treats_disc_01_and_1_as_one_disc():
    kept = local_files("1", disc="01")["01"]["1"]
    moved = local_files("7", numbered=False, disc="1")["1"]["7"]
    mtracks = [source_track("S", "1", 1, 1)]

    with pytest.raises(TrackCombineError, match="would hold two local tracks"):
        _refuse_renumbering_that_loses_a_track([kept, moved], mtracks, [moved], False, True)
