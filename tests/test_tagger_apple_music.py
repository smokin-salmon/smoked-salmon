import anyio
import pytest

from salmon.sources.apple_music import AppleMusicBase
from salmon.tagger.sources.apple_music import Scraper
from salmon.tagger.sources.base import generate_artists


def soup(artist_names: list[str], title: str = "A Track", include_artists: bool = True) -> dict:
    """Build an amp-api shaped soup, with the artists one by one as Apple includes them."""
    artists = [{"type": "artists", "id": f"a{i}", "attributes": {"name": name}} for i, name in enumerate(artist_names)]
    track: dict = {
        "type": "songs",
        "attributes": {
            "name": title,
            "artistName": " & ".join(artist_names),
            "trackNumber": 1,
            "discNumber": 1,
        },
    }
    if include_artists:
        track["relationships"] = {"artists": {"data": artists}}
    return {"album": {"name": "An Album"}, "tracks": [track]}


def test_included_artists_become_distinct_credits_in_apples_order() -> None:
    tracks = anyio.run(Scraper().parse_tracks, soup(["Archie Hamilton", "US Two"]))

    assert tracks["1"][1]["artists"] == [("Archie Hamilton", "main"), ("US Two", "main")]


def test_three_artists_from_a_real_amp_api_response() -> None:
    # 6804372253 ("Mrignaini"): the display string puts the names in one order, Apple's artists in another.
    s = soup(["Sahil Singh", "RYDM", "KAUSHIK"], title="Mrignaini")
    s["tracks"][0]["attributes"]["artistName"] = "Sahil Singh, RYDM & KAUSHIK"

    tracks = anyio.run(Scraper().parse_tracks, s)

    assert tracks["1"][1]["artists"] == [("Sahil Singh", "main"), ("RYDM", "main"), ("KAUSHIK", "main")]


@pytest.mark.parametrize(
    "name",
    ["Simon & Garfunkel", "Earth, Wind & Fire", "Tyler, The Creator", "Kool & The Gang", "AC/DC"],
)
def test_an_artist_whose_name_holds_a_separator_stays_whole(name: str) -> None:
    tracks = anyio.run(Scraper().parse_tracks, soup([name]))

    assert tracks["1"][1]["artists"] == [(name, "main")]


def test_a_display_string_without_included_artists_is_kept_whole() -> None:
    tracks = anyio.run(Scraper().parse_tracks, soup(["Archie Hamilton", "US Two"], include_artists=False))

    assert tracks["1"][1]["artists"] == [("Archie Hamilton & US Two", "main")]


def test_distinct_credits_reach_the_release_artist_list() -> None:
    # scrape_release derives the release's artists from its tracks; distinct credits must survive that step.
    tracks = anyio.run(Scraper().parse_tracks, soup(["Archie Hamilton", "US Two"]))
    artists, _tracks = generate_artists(tracks)

    assert artists == [("Archie Hamilton", "main"), ("US Two", "main")]


def test_a_whole_name_on_another_track_does_not_join_two_explicit_artists() -> None:
    # The bad-split repair would join "A" + "B" into "AB" on the first track. Apple states each artist
    # separately, so the two real artists must survive generate_artists.
    first = soup(["A", "B"], title="First")
    second = soup(["AB"], title="Second")
    second["tracks"][0]["attributes"]["trackNumber"] = 2
    tracks = anyio.run(Scraper().parse_tracks, {"tracks": first["tracks"] + second["tracks"]})

    artists, tracks = generate_artists(tracks)

    assert artists == [("A", "main"), ("B", "main"), ("AB", "main")]
    assert tracks["1"][1]["artists"] == [("A", "main"), ("B", "main")]
    assert tracks["1"][1]["explicit_main_artists"] is True


def test_exemption_survives_repeated_generate_artists_passes() -> None:
    # combine.py, metadata.py and review.py run generate_artists again on the same tracks.
    first = soup(["A", "B"], title="First")
    second = soup(["AB"], title="Second")
    second["tracks"][0]["attributes"]["trackNumber"] = 2
    tracks = anyio.run(Scraper().parse_tracks, {"tracks": first["tracks"] + second["tracks"]})

    artists, tracks = generate_artists(tracks)
    for _ in range(2):
        artists, tracks = generate_artists(tracks)

    assert artists == [("A", "main"), ("B", "main"), ("AB", "main")]
    assert tracks["1"][1]["artists"] == [("A", "main"), ("B", "main")]


def test_title_guests_are_repaired_even_when_mains_are_explicit() -> None:
    # Only relationship-derived mains are exempt. A title's "feat. Simon & Garfunkel" is free text and
    # must rejoin when another track credits that whole name.
    first = soup(["X"], title="Song (feat. Simon & Garfunkel)")
    second = soup(["Simon & Garfunkel"], title="Other")
    second["tracks"][0]["attributes"]["trackNumber"] = 2

    tracks = anyio.run(Scraper().parse_tracks, {"tracks": first["tracks"] + second["tracks"]})
    _artists, tracks = generate_artists(tracks)

    assert tracks["1"][1]["artists"] == [("X", "main"), ("Simon & Garfunkel", "guest")]
    assert tracks["1"][2]["artists"] == [("Simon & Garfunkel", "main")]


def test_a_featured_artist_in_the_relationship_is_a_guest_not_a_main() -> None:
    # Apple can list a song's featured artist alongside its main artists. The title says who is a guest.
    s = soup(["A", "X"], title="Song (feat. X)")
    s["tracks"][0]["attributes"]["artistName"] = "A feat. X"

    tracks = anyio.run(Scraper().parse_tracks, s)

    assert tracks["1"][1]["artists"] == [("A", "main"), ("X", "guest")]
    assert tracks["1"][1]["explicit_main_artists"] is True


def test_an_incomplete_relationship_falls_back_to_the_whole_display_string() -> None:
    # C has no Apple artist page, so Apple leaves it out of the relationship. Keep it in the whole string.
    s = soup(["A", "B"], title="Song")
    s["tracks"][0]["attributes"]["artistName"] = "A, B & C"

    tracks = anyio.run(Scraper().parse_tracks, s)

    assert tracks["1"][1]["artists"] == [("A, B & C", "main")]
    assert "explicit_main_artists" not in tracks["1"][1]


def test_featured_guests_from_the_title_are_still_split() -> None:
    tracks = anyio.run(Scraper().parse_tracks, soup(["Main Artist"], title="Song (feat. One & Two)"))

    assert tracks["1"][1]["artists"] == [("Main Artist", "main"), ("One", "guest"), ("Two", "guest")]


def test_fetch_data_asks_for_each_tracks_artists(monkeypatch) -> None:
    seen: dict[str, dict] = {}

    async def fake_token(cls):
        return "token"

    async def fake_get_json(_self, url, params=None, headers=None):
        key = "tracks" if url.endswith("/tracks") else "album"
        seen[key] = dict(params or {})
        track = {
            "type": "songs",
            "attributes": {"name": "Track 1", "trackNumber": 1},
            "relationships": {
                "artists": {"data": [{"type": "artists", "id": "a1", "attributes": {"name": "An Artist"}}]}
            },
        }
        if key == "album":
            return {
                "data": [
                    {
                        "attributes": {"name": "An Album"},
                        "relationships": {"tracks": {"data": [track], "next": "/v1/catalog/us/albums/1/tracks"}},
                    }
                ]
            }
        return {"data": [track]}

    monkeypatch.setattr(AppleMusicBase, "_get_token", fake_token)
    monkeypatch.setattr(AppleMusicBase, "get_json", fake_get_json)

    result = anyio.run(AppleMusicBase().fetch_data, "https://music.apple.com/us/album/-/6804372253")

    assert seen["album"]["include"] == "tracks,artists"
    assert seen["album"]["include[songs]"] == "artists"
    assert seen["tracks"]["include"] == "artists"
    # The artists stay on the track resource, where parse_tracks reads them.
    assert result["tracks"][0]["relationships"]["artists"]["data"][0]["attributes"]["name"] == "An Artist"
