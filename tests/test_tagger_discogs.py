import asyncio

from salmon.tagger.sources.discogs import Scraper


def make_soup(tracklist: list[dict]) -> dict:
    return {
        "tracklist": tracklist,
        "artists": [{"name": "Various"}],
    }


def track(position: str, title: str, type_: str = "track") -> dict:
    return {"type_": type_, "position": position, "title": title}


def heading(title: str) -> dict:
    return {"type_": "heading", "position": "", "title": title}


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
