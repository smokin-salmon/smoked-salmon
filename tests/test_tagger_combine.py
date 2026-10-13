import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salmon.tagger.combine import (
    _extract_remixers_from_title,
    check_for_artist_fragments,
    combine_metadatas,
    combine_tracks,
)
from salmon.tagger.pre_data import parse_artists
from salmon.tagger.retagger import create_artist_str
from salmon.tagger.sources.discogs import parse_artists as discogs_parse_artists
from salmon.tagger.sources.discogs import parse_credits


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
