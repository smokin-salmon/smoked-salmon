import asyncio
import json

import pytest

from salmon.tagger.sources.base import append_remixers_to_track_titles, generate_artists
from salmon.tagger.sources.discogs import Scraper, _clean_discogs_position, parse_artists, parse_credits


def make_soup(tracklist: list[dict]) -> dict:
    return {
        "tracklist": tracklist,
        "artists": [{"name": "Various"}],
    }


def track(position: str, title: str, type_: str = "track") -> dict:
    return {"type_": type_, "position": position, "title": title}


def heading(title: str) -> dict:
    return {"type_": "heading", "position": "", "title": title}


def test_discogs_arranged_by_maps_to_arranger() -> None:
    artist_soup = [{"name": "Main Artist"}]
    track = {
        "artists": [{"name": "Main Artist"}],
        "extraartists": [{"name": "Some Arranger", "role": "Arranged By"}],
    }

    artists = parse_artists(artist_soup, track)

    assert ("Main Artist", "main") in artists
    assert ("Some Arranger", "arranger") in artists


def test_discogs_artist_who_arranged_their_own_track_stays_its_main_artist() -> None:
    track = {
        "artists": [{"name": "Main Artist"}],
        "extraartists": [{"name": "Main Artist", "role": "Arranged By"}],
    }

    assert parse_artists([], track) == [("Main Artist", "main"), ("Main Artist", "arranger")]


def test_discogs_multi_disc_positions_without_headings_split_by_disc() -> None:
    scraper = Scraper()
    soup = make_soup(
        [
            track("1-01", "Disc One Track One"),
            track("1-02", "Disc One Track Two"),
            track("2-01", "Disc Two Track One"),
            track("2-02", "Disc Two Track Two"),
        ]
    )

    tracks = asyncio.run(scraper.parse_tracks(soup))

    assert set(tracks.keys()) == {"1", "2"}
    assert set(tracks["1"].keys()) == {"1", "2"}
    assert set(tracks["2"].keys()) == {"1", "2"}
    assert tracks["1"]["1"]["title"] == "Disc One Track One"
    assert tracks["1"]["1"]["disc#"] == "1"
    assert tracks["1"]["2"]["title"] == "Disc One Track Two"
    assert tracks["1"]["2"]["disc#"] == "1"
    assert tracks["2"]["1"]["title"] == "Disc Two Track One"
    assert tracks["2"]["1"]["disc#"] == "2"
    assert tracks["2"]["2"]["title"] == "Disc Two Track Two"
    assert tracks["2"]["2"]["disc#"] == "2"


def test_discogs_plain_positions_stay_on_a_single_disc() -> None:
    scraper = Scraper()
    soup = make_soup(
        [
            track("1", "Track One"),
            track("2", "Track Two"),
            track("3", "Track Three"),
        ]
    )

    tracks = asyncio.run(scraper.parse_tracks(soup))

    assert set(tracks.keys()) == {"1"}
    assert set(tracks["1"].keys()) == {"1", "2", "3"}
    assert tracks["1"]["1"]["title"] == "Track One"
    assert tracks["1"]["1"]["disc#"] == "1"
    assert tracks["1"]["3"]["title"] == "Track Three"
    assert tracks["1"]["3"]["disc#"] == "1"


def test_discogs_headings_with_plain_positions_split_where_the_numbering_restarts() -> None:
    scraper = Scraper()
    soup = make_soup(
        [
            heading("Disc 1"),
            track("1", "Disc One Track One"),
            track("2", "Disc One Track Two"),
            heading("Disc 2"),
            track("1", "Disc Two Track One"),
            track("2", "Disc Two Track Two"),
        ]
    )

    tracks = asyncio.run(scraper.parse_tracks(soup))

    assert set(tracks.keys()) == {"1", "2"}
    assert tracks["1"]["1"]["title"] == "Disc One Track One"
    assert tracks["1"]["1"]["disc#"] == "1"
    assert tracks["2"]["1"]["title"] == "Disc Two Track One"
    assert tracks["2"]["1"]["disc#"] == "2"


def test_discogs_vinyl_style_positions_keep_their_side_prefixed_keys() -> None:
    scraper = Scraper()
    soup = make_soup(
        [
            track("A1", "Side A Track One"),
            track("A2", "Side A Track Two"),
            track("B1", "Side B Track One"),
        ]
    )

    tracks = asyncio.run(scraper.parse_tracks(soup))

    assert set(tracks.keys()) == {"1"}
    assert set(tracks["1"].keys()) == {"A1", "A2", "B1"}
    assert tracks["1"]["A1"]["title"] == "Side A Track One"
    assert tracks["1"]["A1"]["disc#"] == "1"
    assert tracks["1"]["B1"]["title"] == "Side B Track One"
    assert tracks["1"]["B1"]["disc#"] == "1"


def discs(tracklist: list[dict]) -> dict[str, list[str]]:
    """Each disc parse_tracks gives, with its track numbers in order."""
    tracks = asyncio.run(Scraper().parse_tracks(make_soup(tracklist)))
    return {disc: list(numbers) for disc, numbers in tracks.items()}


def test_a_vinyl_side_heading_does_not_start_a_disc() -> None:
    # Side letters start again on each side, but one record is one disc (#984, #922).
    tracklist = [heading("Side A"), track("A1", "One"), track("A2", "Two"), heading("Side B"), track("B1", "Three")]

    assert discs(tracklist) == {"1": ["A1", "A2", "B1"]}


def test_a_heading_inside_one_disc_does_not_start_a_disc() -> None:
    # Headings that group one disc's tracks by composer, numbered on through them (Reiner, Vienna).
    tracklist = [heading("Strauss"), track("1", "One"), track("2", "Two"), heading("Lehár"), track("3", "Three")]

    assert discs(tracklist) == {"1": ["1", "2", "3"]}


def test_numbering_back_to_one_starts_a_disc_without_a_heading() -> None:
    assert discs([track("1", "One"), track("2", "Two"), track("1", "Three"), track("2", "Four")]) == {
        "1": ["1", "2"],
        "2": ["1", "2"],
    }


def test_as_before_a_second_record_numbered_from_a1_again_starts_a_disc() -> None:
    tracklist = [heading("LP 1"), track("A1", "One"), track("B1", "Two"), heading("LP 2"), track("A1", "Three")]

    assert discs(tracklist) == {"1": ["A1", "B1"], "2": ["A1"]}


def test_a_box_set_position_names_its_disc() -> None:
    tracklist = [
        heading("CD 1"),
        track("CD1-1", "One"),
        track("CD1-2", "Two"),
        heading("CD 2"),
        track("CD2-1", "Three"),
    ]

    assert discs(tracklist) == {"1": ["1", "2"], "2": ["1"]}


@pytest.mark.parametrize("position", ["CD1-1", "cd1-1", "SACD1-1"])
def test_a_medium_before_the_disc_and_track_is_read(position: str) -> None:
    assert _clean_discogs_position(position) == (1, "1")


def test_a_release_numbered_disc_dot_track_throughout_reads_its_discs() -> None:
    tracklist = [
        heading("Album"),
        track("1.1", "One"),
        track("1.2", "Two"),
        heading("Instrumentals"),
        track("2.1", "Three"),
    ]

    assert discs(tracklist) == {"1": ["1", "2"], "2": ["1"]}


def test_as_before_a_dot_numbering_a_tracks_parts_stays_as_written() -> None:
    # A single CD whose 12th track has parts: "12.1" is no disc.
    assert discs([track("11", "Eleven"), track("12.1", "Part one"), track("12.2", "Part two")]) == {
        "1": ["11", "12.1", "12.2"]
    }


def test_as_before_an_unnumbered_track_before_a1_does_not_start_a_disc() -> None:
    tracklist = [track("-", "Introduction"), track("A1", "One"), track("B1", "Two")]

    assert discs(tracklist) == {"1": ["-", "A1", "B1"]}


def test_as_before_a_video_position_stays_as_written() -> None:
    # Read as a disc, a DVD's entries would take the CD's track numbers.
    assert discs([track("1-1", "One"), track("DVD1-1", "Film")]) == {"1": ["1", "DVD1-1"]}


def test_as_before_a_disc_and_track_with_a_part_letter_stays_on_its_disc() -> None:
    tracklist = [track("1-1", "One"), heading("Disc 2"), track("2-0a", "Intro, part a"), track("2-1", "Two")]

    assert discs(tracklist) == {"1": ["1"], "2": ["2-0A", "1"]}


def test_as_before_a_disc_numbered_0_is_a_disc_of_its_own() -> None:
    assert discs([track("0-1", "Zero"), track("1-1", "One")]) == {"0": ["1"], "1": ["1"]}


def test_as_before_a_second_record_of_one_track_sides_starts_a_disc() -> None:
    tracklist = [heading("LP 1"), track("A", "One"), track("B", "Two"), heading("LP 2"), track("A", "Three")]

    assert discs(tracklist) == {"1": ["A", "B"], "2": ["A"]}


def test_an_unnumbered_track_after_a_heading_leaves_the_restart_to_the_next() -> None:
    # The "-" stays on the disc before; the second record's "A" still starts its own, so none is written over.
    tracklist = [heading("LP 1"), track("A", "One"), track("B", "Two"), heading("LP 2"), track("-", "Intro")]
    tracklist.append(track("A", "Three"))

    assert discs(tracklist) == {"1": ["A", "B", "-"], "2": ["A"]}


def test_as_before_numbering_going_back_after_a_heading_starts_a_disc() -> None:
    # Disc 1's first track has parts, so its "1" is never seen again: the "2" would otherwise replace disc 1's.
    tracklist = [
        heading("CD 1"),
        track("1a", "One"),
        track("2", "Two"),
        heading("CD 2"),
        track("1", "Three"),
        track("2", "Four"),
    ]

    assert discs(tracklist) == {"1": ["1A", "2"], "2": ["1", "2"]}


def test_as_before_a_dvd_numbered_from_1_after_a_heading_takes_no_cd_track() -> None:
    tracklist = [track("1a", "One"), track("2", "Two"), heading("DVD"), track("1", "Film"), track("2", "Extra")]

    assert discs(tracklist) == {"1": ["1A", "2"], "2": ["1", "2"]}


def test_as_before_a_repeated_position_after_the_first_does_not_start_a_disc() -> None:
    # #968: a repeated position is a tracklist error, not a new disc.
    tracklist = [heading("Live"), track("1", "One"), track("2", "Two"), track("2", "Again")]

    assert list(discs(tracklist)) == ["1"]


def test_a_second_record_from_a1_again_starts_a_disc_without_a_heading() -> None:
    assert discs([track("A1", "One"), track("B1", "Two"), track("A1", "Three")]) == {"1": ["A1", "B1"], "2": ["A1"]}


def artist(name: str, join: str = "") -> dict:
    return {"name": name, "join": join}


def test_discogs_feat_join_makes_following_artists_guests() -> None:
    # Discogs release 2520452, track 4-11
    track = {"artists": [artist("Shawfire", "Feat."), artist("Ultra (27)", "&"), artist("Tullisa")]}

    assert parse_artists([{"name": "Various"}], track) == [
        ("Shawfire", "main"),
        ("Ultra", "guest"),
        ("Tullisa", "guest"),
    ]


def test_discogs_feat_join_spellings() -> None:
    for join in ("Featuring", "featuring", "Ft", "ft.", "FEAT", "Feat."):
        track = {"artists": [artist("Lead", join), artist("Guest")]}

        assert parse_artists([], track) == [("Lead", "main"), ("Guest", "guest")], join


def test_discogs_feat_join_applies_to_release_artists_without_track_artists() -> None:
    release_artists = [artist("Lead", "Feat."), artist("Guest")]

    assert parse_artists(release_artists, {}) == [("Lead", "main"), ("Guest", "guest")]


def test_discogs_other_joins_keep_artists_main() -> None:
    for join in ("&", ",", "Vs.", "And", "With", ""):
        track = {"artists": [artist("One", join), artist("Two")]}

        assert parse_artists([], track) == [("One", "main"), ("Two", "main")], join


def test_discogs_feat_join_is_not_matched_inside_a_longer_word() -> None:
    track = {"artists": [artist("One", "Featured"), artist("Two")]}

    assert parse_artists([], track) == [("One", "main"), ("Two", "main")]


def test_discogs_guest_after_feat_join_is_not_added_again_from_extraartists() -> None:
    track = {
        "artists": [artist("Lead", "Feat."), artist("Guest")],
        "extraartists": [
            {"name": "Guest", "role": "Featuring"},
            {"name": "Guest", "role": "Vocals, Featuring"},
        ],
    }

    assert parse_artists([], track) == [("Lead", "main"), ("Guest", "guest")]


def test_discogs_extraartist_with_another_role_is_kept_for_the_same_name() -> None:
    track = {
        "artists": [artist("Lead", "Feat."), artist("Guest")],
        "extraartists": [{"name": "Guest", "role": "Producer"}],
    }

    assert parse_artists([], track) == [("Lead", "main"), ("Guest", "guest"), ("Guest", "producer")]


def test_discogs_asterisk_is_stripped_before_the_numbered_suffix() -> None:
    track = {"artists": [artist("Lead", "Feat."), artist("Guest (27)*")]}

    assert parse_artists([], track) == [("Lead", "main"), ("Guest", "guest")]


def test_discogs_vs_join_keeps_both_artists_main_and_adds_remixers() -> None:
    # Discogs release 2520452, track 1-16
    track = {
        "artists": [artist("Selena (5)", "Vs."), {"name": "X.Men", "anv": "X-Men", "join": ""}],
        "extraartists": [
            {"name": "DJ Face", "anv": "Face", "join": "", "role": "Remix"},
            {"name": 'Matt "Jam" Lamont', "anv": "Jamm", "join": "", "role": "Remix"},
        ],
    }

    assert parse_artists([{"name": 'Matt "Jam" Lamont'}], track) == [
        ("Selena", "main"),
        ("X.Men", "main"),
        ("DJ Face", "remixer"),
        ('Matt "Jam" Lamont', "remixer"),
    ]


def test_discogs_reads_the_anv_of_track_artists_and_extraartists() -> None:
    # Discogs release 2520452, track 4-6 (the Remix credit has no ANV there; "Trouble Men" is given one here)
    track = {
        "artists": [
            {"name": "Gomi", "anv": "DJ Gomi", "join": "Feat."},
            {"name": 'Louie "Balo" Guzman', "anv": "Loui Balo", "join": "&"},
            {"name": "Yasmeen", "anv": "", "join": ""},
        ],
        "extraartists": [{"name": "Trouble Men", "anv": "The Trouble Men", "join": "", "role": "Remix"}],
    }

    artists, variations = parse_credits([], track)

    assert artists == [
        ("Gomi", "main"),
        ('Louie "Balo" Guzman', "guest"),
        ("Yasmeen", "guest"),
        ("Trouble Men", "remixer"),
    ]
    assert variations == [
        {"name": "Gomi", "role": "main", "anv": "DJ Gomi"},
        {"name": 'Louie "Balo" Guzman', "role": "guest", "anv": "Loui Balo"},
        {"name": "Trouble Men", "role": "remixer", "anv": "The Trouble Men"},
    ]


def test_discogs_anv_equal_to_the_name_or_empty_is_not_a_variation() -> None:
    track = {"artists": [{"name": "Ultra (27)", "anv": "Ultra"}, {"name": "Tullisa", "anv": ""}, {"name": "Lead"}]}

    assert parse_credits([], track)[1] == []


def test_discogs_scraper_carries_the_anv_beside_the_track_artists() -> None:
    tracklist = [
        {
            "type_": "track",
            "position": "3-3",
            "title": "Tell Me It's Real",
            "artists": [{"name": "K-Ci & JoJo", "anv": "K-Ci And JoJo", "join": ""}],
        }
    ]

    tracks = asyncio.run(Scraper().parse_tracks(make_soup(tracklist)))

    assert tracks["3"]["3"]["artists"] == [("K-Ci & JoJo", "main")]
    assert tracks["3"]["3"]["artist_variations"] == [{"name": "K-Ci & JoJo", "role": "main", "anv": "K-Ci And JoJo"}]
    # Plain data: a scrape that is shown as JSON must not hold tuple keys.
    json.dumps(tracks)


def test_discogs_remix_credit_does_not_add_a_second_version_to_the_title() -> None:
    # Discogs release 1217780: the title already names its version, the remixer is only credited
    tracklist = [
        {
            "type_": "track",
            "position": "1",
            "title": "Nobody Else (London Connection Edit)",
            "artists": [{"name": "Lead", "anv": "", "join": ""}],
            "extraartists": [{"name": "London Connection", "anv": "", "join": "", "role": "Remix"}],
        }
    ]

    tracks = asyncio.run(Scraper().parse_tracks(make_soup(tracklist)))
    _, tracks = generate_artists(tracks)
    tracks = append_remixers_to_track_titles(tracks)

    assert tracks["1"]["1"]["title"] == "Nobody Else (London Connection Edit)"
    assert ("London Connection", "remixer") in tracks["1"]["1"]["artists"]


def test_a_discogs_position_with_a_superscript_stays_as_written() -> None:
    assert _clean_discogs_position("1²") == (None, "1²")


@pytest.mark.parametrize(("position", "parsed"), [("03", (None, "3")), ("a1", (None, "A1")), ("1-05", (1, "5"))])
def test_a_discogs_position_is_read_as_before(position: str, parsed: tuple[int | None, str]) -> None:
    assert _clean_discogs_position(position) == parsed
