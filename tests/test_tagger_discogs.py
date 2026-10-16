import asyncio
import json

from salmon.tagger.sources.base import append_remixers_to_track_titles, generate_artists
from salmon.tagger.sources.discogs import Scraper, parse_artists, parse_credits


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


def test_discogs_headings_with_plain_positions_still_split_by_heading() -> None:
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
