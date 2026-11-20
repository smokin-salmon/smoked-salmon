import asyncio
import json
import random

import pytest

from salmon.tagger.sources.base import append_remixers_to_track_titles, generate_artists
from salmon.tagger.sources.discogs import (
    Scraper,
    _clean_discogs_position,
    _disc_starts,
    _disc_starts_on,
    parse_artists,
    parse_credits,
)


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


def discs(tracklist: list[dict], formats: list[dict] | None = None) -> dict[str, list[str]]:
    """Each disc parse_tracks gives, with its track numbers in order."""
    soup = make_soup(tracklist) if formats is None else {**make_soup(tracklist), "formats": formats}
    tracks = asyncio.run(Scraper().parse_tracks(soup))
    return {disc: list(numbers) for disc, numbers in tracks.items()}


def test_a_vinyl_side_heading_does_not_start_a_disc() -> None:
    # Side letters start again on each side, but one record is one disc (#984, #922).
    tracklist = [heading("Side A"), track("A1", "One"), track("A2", "Two"), heading("Side B"), track("B1", "Three")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["A1", "A2", "B1"]}


def test_a_heading_inside_one_disc_does_not_start_a_disc() -> None:
    # Headings that group one disc's tracks by composer, numbered on through them (Reiner, Vienna).
    tracklist = [heading("Strauss"), track("1", "One"), track("2", "Two"), heading("Lehár"), track("3", "Three")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["1", "2", "3"]}


def test_numbering_back_to_one_starts_a_disc_without_a_heading() -> None:
    parsed = discs([track("1", "One"), track("2", "Two"), track("1", "Three"), track("2", "Four")])

    assert parsed == {
        "1": ["1", "2"],
        "2": ["1", "2"],
    }


def test_as_before_a_second_record_numbered_from_a1_again_starts_a_disc() -> None:
    tracklist = [heading("LP 1"), track("A1", "One"), track("B1", "Two"), heading("LP 2"), track("A1", "Three")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["A1", "B1"], "2": ["A1"]}


def test_a_box_set_position_names_its_disc() -> None:
    tracklist = [
        heading("CD 1"),
        track("CD1-1", "One"),
        track("CD1-2", "Two"),
        heading("CD 2"),
        track("CD2-1", "Three"),
    ]

    parsed = discs(tracklist)

    assert parsed == {"1": ["1", "2"], "2": ["1"]}


@pytest.mark.parametrize("position", ["CD1-1", "cd1-1", "SACD1-1"])
def test_a_medium_before_the_disc_and_track_is_read(position: str) -> None:
    assert _clean_discogs_position(position) == (1, "1")


TWO_CDS = [{"name": "CD", "qty": "2", "descriptions": ["Album"]}]


def test_a_release_numbered_disc_dot_track_throughout_reads_its_discs() -> None:
    tracklist = [
        heading("Album"),
        track("1.1", "One"),
        track("1.2", "Two"),
        heading("Instrumentals"),
        track("2.1", "Three"),
    ]

    parsed = discs(tracklist, TWO_CDS)

    assert parsed == {"1": ["1", "2"], "2": ["1"]}


def test_as_before_a_second_disc_whose_first_track_has_parts_starts_after_a_heading() -> None:
    tracklist = [track("1", "One"), track("2", "Two"), heading("CD 2"), track("1a", "Three, part a")]
    tracklist += [track("1b", "Three, part b"), track("2", "Four")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["1", "2"], "2": ["1A", "1B", "2"]}


def test_a_part_numbered_on_after_a_heading_stays_on_its_disc() -> None:
    parsed = discs([track("1", "One"), track("2", "Two"), heading("Suite"), track("3a", "Three, part a")])

    assert parsed == {"1": ["1", "2", "3A"]}


def test_a_track_s_next_part_after_a_heading_stays_on_its_disc() -> None:
    parsed = discs([track("1a", "One, part a"), heading("Interlude"), track("1b", "One, part b"), track("2", "Two")])

    assert parsed == {"1": ["1A", "1B", "2"]}


@pytest.mark.parametrize("formats", [None, [{"name": "CD", "qty": "2"}]])
def test_as_before_one_work_numbered_by_its_parts_stays_as_written(formats: list[dict] | None) -> None:
    # "1.1", "1.2": one track's parts, not disc 1's tracks, even on two CDs.
    parsed = discs([track("1.1", "Part one"), track("1.2", "Part two")], formats)

    assert parsed == {"1": ["1.1", "1.2"]}


def test_as_before_dots_on_only_some_tracks_stay_as_written() -> None:
    # Parts of the first two tracks, then a plain one: the dots number parts, not discs.
    parsed = discs([track("1.1", "One, part one"), track("2.1", "Two, part one"), track("3", "Three")])

    assert parsed == {"1": ["1.1", "2.1", "3"]}


def test_an_unnumbered_entry_leaves_a_release_numbered_disc_dot_track() -> None:
    tracklist = [track("1.1", "One"), track("1.2", "Two"), heading("CD 2"), track("-", "Intro"), track("2.1", "Three")]

    parsed = discs(tracklist, TWO_CDS)

    assert parsed == {"1": ["1", "2", "-"], "2": ["1"]}


MOVEMENTS = [track("1.1", "I"), track("1.2", "II"), track("2.1", "I"), track("2.2", "II")]


@pytest.mark.parametrize(
    "formats",
    [
        [{"name": "CD", "qty": "1"}],
        [{"name": "CD"}],
        [{"name": "CD", "qty": ""}],
        [],
        None,
        # Sides are one disc (#984), and a file release's qty counts its files.
        [{"name": "Cassette", "qty": "1"}],
        [{"name": "Vinyl", "qty": "2"}],
        [{"name": "File", "qty": "12"}],
        [{"name": "CD", "qty": "1"}, {"name": "DVD", "qty": "1"}],
    ],
)
def test_as_before_dots_on_a_release_of_one_cd_or_none_number_works_and_stay_as_written(
    formats: list[dict] | None,
) -> None:
    # Two works' movements on one CD, not two discs: the formats say how many CDs there are.
    parsed = discs(MOVEMENTS, formats)

    assert parsed == {"1": ["1.1", "1.2", "2.1", "2.2"]}


@pytest.mark.parametrize(
    "formats",
    [
        TWO_CDS,
        [{"name": "Box Set", "qty": "1"}, {"name": "CD", "qty": "2"}],
        [{"name": "CD", "qty": "1"}, {"name": "CDr", "qty": "1"}],
        [{"name": "CD"}, {"name": "CD"}],
        [{"name": "SACD", "qty": "2"}],
    ],
)
def test_dots_on_a_release_of_more_than_one_cd_read_as_disc_and_track(formats: list[dict]) -> None:
    parsed = discs(MOVEMENTS, formats)

    assert parsed == {"1": ["1", "2"], "2": ["1", "2"]}


def test_as_before_dots_naming_more_discs_than_the_formats_state_number_movements() -> None:
    # Two CDs of works numbered by their movements, "1.1" to "3.2": no third disc.
    tracklist = [*MOVEMENTS, track("3.1", "I"), track("3.2", "II")]

    parsed = discs(tracklist, TWO_CDS)

    assert parsed == {"1": ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2"]}


def test_dots_naming_a_video_disc_beside_the_cds_read_it_as_a_disc_too() -> None:
    tracklist = [*MOVEMENTS, track("3.1", "Film")]

    parsed = discs(tracklist, [{"name": "CD", "qty": "2"}, {"name": "DVD", "qty": "1"}])

    assert parsed == {"1": ["1", "2"], "2": ["1", "2"], "3": ["1"]}


def test_as_before_dots_repeated_on_each_cd_number_movements_on_two_discs() -> None:
    # Each CD numbers its works from "1.1": as disc and track, the second CD's would replace the first's.
    tracklist = [heading("CD 1"), *MOVEMENTS, heading("CD 2"), *MOVEMENTS]

    parsed = discs(tracklist, TWO_CDS)

    assert parsed == {"1": ["1.1", "1.2", "2.1", "2.2"], "2": ["1.1", "1.2", "2.1", "2.2"]}


def test_as_before_a_dot_numbering_a_tracks_parts_stays_as_written() -> None:
    # A single CD whose 12th track has parts: "12.1" is no disc.
    parsed = discs([track("11", "Eleven"), track("12.1", "Part one"), track("12.2", "Part two")])

    assert parsed == {"1": ["11", "12.1", "12.2"]}


def test_as_before_an_unnumbered_track_before_a1_does_not_start_a_disc() -> None:
    tracklist = [track("-", "Introduction"), track("A1", "One"), track("B1", "Two")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["-", "A1", "B1"]}


def test_as_before_a_video_position_stays_as_written() -> None:
    # Read as a disc, a DVD's entries would take the CD's track numbers.
    parsed = discs([track("1-1", "One"), track("DVD1-1", "Film")])

    assert parsed == {"1": ["1", "DVD1-1"]}


def test_as_before_a_disc_and_track_with_a_part_letter_stays_on_its_disc() -> None:
    tracklist = [track("1-1", "One"), heading("Disc 2"), track("2-0a", "Intro, part a"), track("2-1", "Two")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["1"], "2": ["2-0A", "1"]}


def test_as_before_a_disc_numbered_0_is_a_disc_of_its_own() -> None:
    parsed = discs([track("0-1", "Zero"), track("1-1", "One")])

    assert parsed == {"0": ["1"], "1": ["1"]}


def test_as_before_a_second_record_of_one_track_sides_starts_a_disc() -> None:
    tracklist = [heading("LP 1"), track("A", "One"), track("B", "Two"), heading("LP 2"), track("A", "Three")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["A", "B"], "2": ["A"]}


@pytest.mark.parametrize(
    ("tracklist", "parsed"),
    [
        # The second record's "A" again.
        (
            [heading("LP 1"), track("A", "One"), track("B", "Two"), heading("LP 2"), track("-", "Intro")]
            + [track("A", "Three")],
            {"1": ["A", "B"], "2": ["-", "A"]},
        ),
        # Numbering going back: "1" after "1a".
        (
            [track("1a", "One"), track("2", "Two"), heading("CD 2"), track("-", "Intro"), track("1", "Three")],
            {"1": ["1A", "2"], "2": ["-", "1"]},
        ),
    ],
)
def test_as_before_an_unnumbered_entry_after_a_heading_goes_to_the_disc_the_next_entry_starts(
    tracklist: list[dict], parsed: dict[str, list[str]]
) -> None:
    # The next entry shows the heading started a disc, so the "-" under it is that disc's too.
    result = discs(tracklist)

    assert result == parsed


@pytest.mark.parametrize(
    ("tracklist", "parsed"),
    [
        (
            [
                heading("1"),
                track("A", "One"),
                track("B", "Two"),
                heading("2"),
                track("AA", "Three"),
                track("B", "Four"),
            ],
            {"1": ["A", "B"], "2": ["AA", "B"]},
        ),
        (
            [track("1", "One"), track("2", "Two"), track("3", "Three"), heading("2")]
            + [track("4", "Four"), track("2", "Five")],
            {"1": ["1", "2", "3"], "2": ["4", "2"]},
        ),
        # A position moved to the new disc, repeated across the next heading.
        (
            [heading("1"), track("A", "One"), track("B", "Two"), heading("2"), track("AA", "Three"), track("B", "Four")]
            + [heading("3"), track("AA", "Five")],
            {"1": ["A", "B"], "2": ["AA", "B"], "3": ["AA"]},
        ),
    ],
)
def test_as_before_a_position_repeated_later_under_a_heading_starts_a_disc(
    tracklist: list[dict], parsed: dict[str, list[str]]
) -> None:
    # The repeat shows the heading started a disc, so what came under it before the repeat goes there too.
    result = discs(tracklist)

    assert result == parsed


@pytest.mark.parametrize(
    ("tracklist", "parsed"),
    [
        (
            [track("1", "One"), track("2", "Two"), heading("Bonus"), track("3", "Three"), track("1", "Four")]
            + [track("2", "Five"), track("3", "Six")],
            {"1": ["1", "2", "3"], "2": ["1", "2", "3"]},
        ),
        (
            [track("1a", "One"), track("2", "Two"), heading("Bonus"), track("3", "Three"), track("1a", "Four")]
            + [track("2", "Five")],
            {"1": ["1A", "2", "3"], "2": ["1A", "2"]},
        ),
        # An intro before the disc's first number.
        (
            [track("-", "Intro"), track("1", "One"), track("2", "Two"), heading("Bonus"), track("3", "Three")]
            + [track("1", "Four"), track("2", "Five"), track("3", "Six")],
            {"1": ["-", "1", "2", "3"], "2": ["1", "2", "3"]},
        ),
    ],
)
# Two CDs stated, and a disc started by the positions: the bonus heading still starts none.
@pytest.mark.parametrize("formats", [None, [{"name": "CD", "qty": "2"}]])
def test_numbering_starting_over_later_under_a_heading_moves_no_track(
    tracklist: list[dict], parsed: dict[str, list[str]], formats: list[dict] | None
) -> None:
    # The disc's first position again: the next disc starts there, with no heading of its own after a bonus track.
    result = discs(tracklist, formats)

    assert result == parsed


def test_a_disc_started_with_no_heading_takes_no_track_from_the_disc_before() -> None:
    parsed = discs([track("1a", "One"), track("2", "Two"), track("1", "Three"), track("3", "Four"), track("1", "Five")])

    assert parsed == {"1": ["1A", "2", "1", "3"], "2": ["1"]}


def test_as_before_a_track_moved_to_the_disc_its_heading_starts_names_that_disc() -> None:
    soup = make_soup([heading("1"), track("A", "One"), track("B", "Two"), heading("2"), track("AA", "Three")])
    soup["tracklist"].append(track("B", "Four"))

    tracks = asyncio.run(Scraper().parse_tracks(soup))

    assert tracks["2"]["AA"]["disc#"] == "2"


def test_unnumbered_entries_under_one_heading_start_no_disc() -> None:
    # The repeats write over the entry before them, as on master; #968 (held) refuses them.
    tracklist = [track("1", "One"), track("2", "Two"), track("3", "Three"), heading("Bonus")]
    tracklist += [track("", "Four"), track("", "Five"), track("", "Six")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["1", "2", "3", ""]}


def test_an_unnumbered_entry_repeated_later_under_a_heading_starts_no_disc() -> None:
    tracklist = [track("1", "One"), track("2", "Two"), track("-", "Intro"), heading("Live"), track("3", "Three")]
    tracklist += [track("-", "Interlude"), track("4", "Four")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["1", "2", "-", "3", "4"]}


@pytest.mark.parametrize(
    ("tracklist", "parsed"),
    [
        # A record numbered from "A1" again with no heading: none came under the disc's own heading.
        (
            [heading("LP 1"), track("A1", "One"), track("B1", "Two"), track("A1", "Three"), track("B1", "Four")]
            + [track("A1", "Five")],
            {"1": ["A1", "B1"], "2": ["A1", "B1"], "3": ["A1"]},
        ),
        # A position naming its disc: the tracks before it stay on theirs.
        (
            [track("1", "One"), heading("Bonus"), track("2", "Two"), track("2-1", "Three"), track("1", "Four")],
            {"1": ["1", "2"], "2": ["1"], "3": ["1"]},
        ),
    ],
)
def test_a_disc_started_since_the_latest_heading_takes_no_track_from_the_disc_before(
    tracklist: list[dict], parsed: dict[str, list[str]]
) -> None:
    result = discs(tracklist)

    assert result == parsed


def test_as_before_unnumbered_entries_each_under_a_heading_keep_their_tracks() -> None:
    parsed = discs([heading("One"), track("", "One"), heading("Two"), track("", "Two")])

    assert parsed == {"1": [""], "2": [""]}


def test_as_before_numbering_going_back_later_under_a_heading_starts_no_disc() -> None:
    # Only a heading's first numbered track starts a disc by going back: "2:4" comes later, so stays on disc 2.
    tracklist = [track("2-1", "One"), track("2-2", "Two"), heading("Bonus"), track("2-3", "Three")]
    tracklist.append(track("2:4", "Four"))

    parsed = discs(tracklist)

    assert parsed == {"2": ["1", "2", "3", "2:4"]}


def test_a_heading_s_first_number_after_the_ninth_part_goes_on_by_its_number() -> None:
    # "1.10" follows "1.9": it goes on, though it sorts before it as text.
    tracklist = [*(track(f"1.{part}", f"Part {part}") for part in range(1, 10)), heading("Finale")]
    tracklist.append(track("1.10", "Part 10"))

    parsed = discs(tracklist)

    assert parsed == {"1": [*(f"1.{part}" for part in range(1, 11))]}


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

    parsed = discs(tracklist)

    assert parsed == {"1": ["1A", "2"], "2": ["1", "2"]}


def test_as_before_a_dvd_numbered_from_1_after_a_heading_takes_no_cd_track() -> None:
    tracklist = [track("1a", "One"), track("2", "Two"), heading("DVD"), track("1", "Film"), track("2", "Extra")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["1A", "2"], "2": ["1", "2"]}


def test_as_before_a_repeated_position_after_the_first_does_not_start_a_disc() -> None:
    # #968: repeated under the same heading, a position is a tracklist error, not a new disc.
    tracklist = [heading("Live"), track("1", "One"), track("2", "Two"), track("2", "Again")]

    parsed = discs(tracklist)

    assert list(parsed) == ["1"]


def test_a_second_record_from_a1_again_starts_a_disc_without_a_heading() -> None:
    parsed = discs([track("A1", "One"), track("B1", "Two"), track("A1", "Three")])

    assert parsed == {"1": ["A1", "B1"], "2": ["A1"]}


def artist(name: str, join: str = "") -> dict:
    return {"name": name, "join": join}


def test_a_track_naming_its_disc_stays_on_it_when_a_repeat_after_it_starts_a_disc() -> None:
    # "1-1" names disc 1: the repeat of "1" after it starts disc 2 there, not at the heading above "1-1".
    tracklist = [track("2", "Two"), heading("More"), track("1-1", "One"), track("1", "One again")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["2", "1"], "2": ["1"]}


def test_a_heading_after_tracks_naming_their_disc_starts_the_next_at_a_repeat() -> None:
    tracklist = [track("1-1", "One"), track("1-2", "Two"), heading("Disc 2"), track("3", "Three"), track("2", "Two")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["1", "2"], "2": ["3", "2"]}


def test_discs_named_out_of_order_keep_every_track_when_numbering_starts_over() -> None:
    # "2-2" before "1-2": disc 2 began before the heading, so the "1"s after it start discs from there, not from it.
    tracklist = [track("2-2", "a"), track("1-2", "b"), track("1a", "c"), track("3", "d"), heading("x")]
    tracklist += [track("1", "e"), track("1", "f")]

    parsed = discs(tracklist)

    assert parsed == {"2": ["2", "1"], "1": ["2", "1A", "3"], "3": ["1"]}


def test_a_track_moved_to_the_next_disc_is_no_repeat_on_the_disc_before() -> None:
    # "3" went to disc 2 with its heading, so a "3" back on disc 1, after "1-4", is disc 1's first.
    tracklist = [track("1", "a"), track("2", "b"), heading("x"), track("3", "c"), track("2", "d"), heading("y")]
    tracklist += [track("1-4", "e"), track("3", "f")]

    parsed = discs(tracklist)

    assert parsed == {"1": ["1", "2", "4", "3"], "2": ["3", "2"]}


def _running_on(title: str, first: list[dict]) -> list[dict]:
    """Numbering that runs on across a heading: "1" to "3", the heading, "4" to "6"."""
    tracklist = [*first, *(track(str(n), f"t{n}") for n in (1, 2, 3)), heading(title)]
    return tracklist + [track(str(n), f"t{n}") for n in (4, 5, 6)]


@pytest.mark.parametrize("title", ["CD 2", "Bonus Disc"])
@pytest.mark.parametrize(
    "first", [[], [heading("CD 1")], [{"type_": "index", "position": "", "title": "w"}, heading("x")]]
)
def test_a_heading_starts_a_disc_when_the_numbering_runs_on_and_the_formats_state_the_cds(
    title: str, first: list[dict]
) -> None:
    parsed = discs(_running_on(title, first), [{"name": "CD", "qty": "2"}])

    assert parsed == {"1": ["1", "2", "3"], "2": ["4", "5", "6"]}


@pytest.mark.parametrize(
    "formats",
    [[{"name": "File", "qty": "7"}], [{"name": "All Media", "qty": "1"}], [{"name": "Box Set"}, {"name": "File"}]],
)
def test_each_heading_starts_a_disc_when_the_numbering_runs_on_and_the_formats_name_no_medium(
    formats: list[dict],
) -> None:
    # A file release's qty counts its files: as on master, since a split costs less at the merge than one disc.
    tracklist = [*_running_on("Live at A", []), heading("Live at B"), track("7", "t7")]

    assert discs(tracklist, formats) == {"1": ["1", "2", "3"], "2": ["4", "5", "6"], "3": ["7"]}


def test_headings_in_a_row_start_one_disc_by_the_stated_cds() -> None:
    tracklist = [track("1", "a"), heading("CD 2"), heading("Act 2"), track("2", "b"), heading("CD 3"), track("3", "c")]

    assert discs(tracklist, [{"name": "CD", "qty": "3"}]) == {"1": ["1"], "2": ["2"], "3": ["3"]}


@pytest.mark.parametrize("formats", [None, [{"name": "CD", "qty": "1"}], [{"name": "Vinyl", "qty": "2"}]])
def test_headings_the_numbering_runs_on_across_start_no_disc_unless_the_formats_state_more_cds(
    formats: list[dict] | None,
) -> None:
    tracklist = [*_running_on("Side B", []), heading("Side C"), track("7", "t7")]

    assert discs(tracklist, formats) == {"1": ["1", "2", "3", "4", "5", "6", "7"]}


@pytest.mark.parametrize(
    ("tracklist", "parsed"),
    [
        # A bonus heading after disc 2's: the first heading starts disc 2, and the bonus tracks stay on it.
        (
            [heading("CD 1"), track("1", "a"), heading("CD 2"), track("2", "b"), heading("Bonus"), track("3", "c")],
            {"1": ["1"], "2": ["2", "3"]},
        ),
        # A bonus heading before disc 2's: the bonus tracks go to disc 2, the files keeping their numbers at the merge.
        (
            [track("1", "a"), heading("Bonus"), track("2", "b"), heading("CD 2"), track("3", "c")],
            {"1": ["1"], "2": ["2", "3"]},
        ),
        # An index entry, a work's movements, ending disc 1.
        (
            [track("1", "a"), {"type_": "index", "position": "", "title": "w"}, heading("CD 2"), track("2", "b")],
            {"1": ["1"], "2": ["2"]},
        ),
    ],
)
@pytest.mark.parametrize("formats", [[{"name": "CD", "qty": "2"}], [{"name": "Box Set"}, {"name": "CD", "qty": "2"}]])
def test_only_the_first_headings_start_discs_up_to_the_stated_cds(
    tracklist: list[dict], parsed: dict[str, list[str]], formats: list[dict]
) -> None:
    assert discs(tracklist, formats) == parsed


@pytest.mark.parametrize(
    "tracklist",
    [
        [heading("x")],
        [track("1", "a"), track("2", "b"), heading("x")],
        [track("1-1", "a"), track("1-2", "b"), heading("x"), track("3", "c")],
    ],
)
def test_a_heading_with_no_track_after_it_or_by_a_position_naming_its_disc_starts_none_by_the_stated_cds(
    tracklist: list[dict],
) -> None:
    # A position naming its disc ("1-1") leaves the discs to the positions.
    soup = {**make_soup(tracklist), "formats": [{"name": "CD", "qty": "2"}]}

    assert _disc_starts_on(soup, dotted=False) == {}


@pytest.mark.parametrize(
    ("tracklist", "formats", "parsed"),
    [
        # Three CDs: "1" again starts disc 2, and the first heading after it disc 3, with every track kept.
        (
            [track("1", "a"), track("2", "b"), track("1", "c"), track("2", "d"), heading("x"), track("3", "e")]
            + [heading("y"), track("4", "f")],
            [{"name": "CD", "qty": "3"}],
            {"1": ["1", "2"], "2": ["1", "2"], "3": ["3", "4"]},
        ),
        # Disc 3 numbered on from disc 2.
        (
            [heading("CD 1"), track("1", "a"), track("2", "b"), heading("CD 2"), track("1", "c"), track("2", "d")]
            + [heading("CD 3"), track("3", "e"), track("4", "f")],
            [{"name": "CD", "qty": "3"}],
            {"1": ["1", "2"], "2": ["1", "2"], "3": ["3", "4"]},
        ),
        # Video discs count: two DVD-Audio discs, numbered on.
        (
            [track("1", "a"), track("2", "b"), heading("Disc 2"), track("3", "c")],
            [{"name": "DVD", "qty": "2"}],
            {"1": ["1", "2"], "2": ["3"]},
        ),
    ],
)
def test_headings_start_the_discs_the_positions_leave_short_of_the_stated_count(
    tracklist: list[dict], formats: list[dict], parsed: dict[str, list[str]]
) -> None:
    assert discs(tracklist, formats) == parsed


def test_a_disc_started_at_a_heading_by_the_stated_cds_gives_its_reason() -> None:
    soup = {**make_soup(_running_on("CD 2", [])), "formats": [{"name": "CD", "qty": "2"}]}

    assert _disc_starts_on(soup, dotted=False) == {3: "a heading, with 2 discs stated"}


@pytest.mark.parametrize(
    ("tracklist", "starts"),
    [
        ([track("1", "a"), track("2", "b"), track("1", "c")], {2: "'1' again"}),
        (
            [heading("x"), track("A", "a"), track("B", "b"), heading("y"), track("AA", "c"), track("B", "d")],
            {3: "'B' again after a heading"},
        ),
        (
            [heading("x"), track("1a", "a"), track("2", "b"), heading("y"), track("1", "c")],
            {3: "numbering back to '1' after a heading"},
        ),
        (
            [track("1", "a"), track("2", "b"), heading("Bonus"), track("3", "c"), track("1", "d")],
            {4: "'1' again"},
        ),
    ],
)
def test_each_disc_start_gives_its_place_and_reason(tracklist: list[dict], starts: dict) -> None:
    assert _disc_starts(tracklist, dotted=False) == starts


# What holds for every tracklist, over many generated ones: a reader can rely on these without the rules above.

_POSITIONS = [
    "1",
    "2",
    "3",
    "01",
    "1a",
    "1b",
    "2a",
    "A1",
    "A2",
    "B1",
    "A",
    "B",
    "AA",
    "-",
    "",
    "1-1",
    "1-2",
    "2-1",
    "2-2",
]


def _generated(count: int) -> list[list[dict]]:
    """Tracklists of positions drawn at random, headings among them, each track titled by its place."""
    rng = random.Random(984)
    tracklists = []
    for _ in range(count):
        choices = rng.choices([*_POSITIONS, "[H]", "[H]"], k=rng.randint(1, 9))
        tracklists.append([heading("h") if c == "[H]" else track(c, f"t{i}") for i, c in enumerate(choices)])
    return tracklists


def _named(entry: dict) -> tuple[int | None, str]:
    return _clean_discogs_position(entry["position"]) if entry["type_"] == "track" else (None, "")


def test_a_track_whose_position_names_its_disc_is_never_on_another() -> None:
    # It may be written over by its own number again on that disc, which #968 refuses.
    for tracklist in _generated(2000):
        tracks = asyncio.run(Scraper().parse_tracks(make_soup(tracklist)))
        disc_of = {track["title"]: disc for disc, numbers in tracks.items() for track in numbers.values()}

        for entry in tracklist:
            disc, _ = _named(entry)
            if disc is not None:
                assert disc_of.get(entry["title"], str(disc)) == str(disc), tracklist


def test_distinct_positions_are_all_kept_in_order_and_with_no_heading_on_one_disc() -> None:
    for tracklist in _generated(2000):
        numbers = [_named(entry)[1] for entry in tracklist if entry["type_"] == "track"]
        if len(set(numbers)) < len(numbers) or any(_named(entry)[0] is not None for entry in tracklist):
            continue

        parsed = discs(tracklist)

        assert [number for disc in parsed.values() for number in disc] == numbers, tracklist
        assert len(parsed) == 1 or any(entry["type_"] == "heading" for entry in tracklist), tracklist


def test_the_disc_starts_come_in_tracklist_order() -> None:
    # Placing tracks counts the starts in order: one out of order, or two at one place, would lose a disc.
    for tracklist in _generated(2000):
        starts = _disc_starts(tracklist, dotted=False)

        assert list(starts) == sorted(starts), tracklist


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
