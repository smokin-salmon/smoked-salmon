"""How a MusicBrainz release's JSON is read: credits, track titles, remixers, barcode, type, and the fields the JSON
leaves empty where the XML left them out (#1065). The requests themselves: tests/test_sources_musicbrainz_client.py.
"""

import inspect

import anyio
import pytest
from musicbrainzngs import musicbrainz as mb_module

from salmon.sources import musicbrainz
from salmon.tagger.sources import musicbrainz as musicbrainz_tagger


def test_safe_read_keeps_the_signature_the_wrapper_calls() -> None:
    # The bound wraps a private function of musicbrainzngs: an upgrade that changes it must fail here, not quietly.
    parameters = list(inspect.signature(musicbrainz._safe_read).parameters)

    assert parameters[:3] == ["opener", "req", "body"]
    assert "max_retries" in parameters
    assert mb_module._safe_read is musicbrainz._bounded_safe_read


def _credit(name: str, joinphrase: str = "") -> dict:
    return {"name": name, "joinphrase": joinphrase, "artist": {"name": name}}


@pytest.mark.parametrize("join", [" feat. ", " ft. ", " featuring ", " Feat. ", " ft "])
def test_a_featured_join_makes_the_artists_after_it_guests(join) -> None:
    # Only " feat. " did: B after " ft. " or " featuring " was a main artist (#910).
    credits = [_credit("A", join), _credit("B")]

    assert musicbrainz_tagger.parse_artists(credits) == [("A", "main"), ("B", "guest")]


@pytest.mark.parametrize("join", [" & ", ", ", " x ", " with "])
def test_another_join_keeps_the_artists_after_it_main(join) -> None:
    credits = [_credit("A", join), _credit("B")]

    assert musicbrainz_tagger.parse_artists(credits) == [("A", "main"), ("B", "main")]


def _track(number: str, recording_title: str, title: str) -> dict:
    return {
        "number": number,
        "title": title,
        "recording": {"title": recording_title, "artist-credit": [_credit("Spice Girls")]},
    }


def test_a_track_takes_its_release_track_title_not_its_recordings() -> None:
    # Disc 14 of MusicBrainz release https://musicbrainz.org/release/9c21406a-2032-4336-b0cd-ecb701cee16b: the track
    # list says "(demo)" on every track, while its recordings say "(demo version)", or nothing and carry "demo" in
    # their disambiguation.
    disc = {
        "position": 14,
        "tracks": [
            _track("2", "Wannabe (demo)", "Wannabe (demo)"),
            _track("3", "2 Become 1", "2 Become 1 (demo)"),
            _track("6", "Step to Me (demo version)", "Step to Me (demo)"),
        ],
    }

    tracks = anyio.run(musicbrainz_tagger.Scraper().parse_tracks, {"media": [disc]})

    assert {number: track["title"] for number, track in tracks["14"].items()} == {
        "2": "Wannabe (demo)",
        "3": "2 Become 1 (demo)",
        "6": "Step to Me (demo)",
    }


# Tracks of MusicBrainz release https://musicbrainz.org/release/ec3baf33-2d0f-4eea-a3ec-8bcd582bf939
# (Lady Gaga, The Remix)
def _remix_track(number: str, recording_title: str, relations: list[tuple[str, str]]) -> dict:
    recording: dict = {"title": recording_title, "artist-credit": [_credit("Lady Gaga")]}
    if relations:
        recording["relations"] = [
            {"type": relation_type, "target-type": "artist", "direction": "backward", "artist": {"name": name}}
            for relation_type, name in relations
        ]
    return {"number": number, "title": recording_title, "recording": recording}


def test_a_recordings_remixer_relation_is_a_remixer_credit() -> None:
    disc = {
        "position": 1,
        "tracks": [
            _remix_track("1", "Just Dance (Richard Vission remix)", [("remixer", "Richard “Humpty” Vission")]),
            _remix_track("2", "Poker Face (LLG vs. GLG radio mix remix)", []),
            _remix_track(
                "9",
                "Alejandro (Sounds of Arrow remix)",
                [("producer", "Lady Gaga"), ("producer", "RedOne"), ("remixer", "The Sound of Arrows")],
            ),
        ],
    }

    tracks = anyio.run(musicbrainz_tagger.Scraper().parse_tracks, {"media": [disc]})["1"]

    assert tracks["1"]["artists"] == [("Lady Gaga", "main"), ("Richard “Humpty” Vission", "remixer")]
    assert tracks["1"]["credited_remixers"] == ["Richard “Humpty” Vission"]
    assert tracks["2"]["artists"] == [("Lady Gaga", "main")]
    assert tracks["2"]["credited_remixers"] == []
    assert tracks["9"]["artists"] == [("Lady Gaga", "main"), ("The Sound of Arrows", "remixer")]
    assert tracks["9"]["credited_remixers"] == ["The Sound of Arrows"]


@pytest.mark.parametrize(
    ("release", "upc"),
    [
        ({"barcode": "00602527462943"}, "00602527462943"),
        ({"barcode": ""}, None),  # MusicBrainz sends "" for a release with no barcode
        ({"barcode": None}, None),
        ({}, None),
    ],
)
def test_a_releases_barcode_is_its_upc(release, upc) -> None:
    assert musicbrainz_tagger.Scraper().parse_upc(release) == upc


@pytest.mark.parametrize(
    ("primary", "secondaries", "expected"),
    [
        ("Album", [], "Album"),
        ("Album", ["Live"], "Live"),
        ("Album", ["Compilation", "DJ-mix"], "Compilation"),
        # The XML's type takes a secondary type only over Album: release 4c1dd3a5-ada8-4e6a-ab6e-b129f6758b9b
        # (Single + Live) was "Single", d4b8af18-da19-48c3-b5bb-d6254fd1510a (EP + Soundtrack) "EP".
        ("Single", ["Live"], "Single"),
        ("EP", ["Soundtrack"], "EP"),
        # Release 2f35c487-1f25-4179-a815-0713bb940d40: no type at all, and no type attribute in the XML.
        (None, [], None),
    ],
)
def test_a_release_groups_type_is_the_one_the_xml_gave(primary, secondaries, expected) -> None:
    group = {"primary-type": primary, "secondary-types": secondaries}

    assert musicbrainz_tagger.release_group_type(group) == expected


@pytest.mark.parametrize(
    ("release", "parse", "expected"),
    [
        ({"label-info": [{"label": None, "catalog-number": "X1"}]}, "parse_release_label", None),
        ({"label-info": [{"label": {"name": "Columbia"}, "catalog-number": None}]}, "parse_release_catno", None),
        ({"label-info": [{"label": None, "catalog-number": "X1"}]}, "parse_release_catno", "X1"),
        ({"label-info": []}, "parse_release_label", None),
        ({"release-events": [{"date": ""}]}, "parse_release_date", None),
        ({"release-events": []}, "parse_release_date", None),
        ({"release-group": {"primary-type": None, "secondary-types": []}}, "parse_release_type", None),
    ],
)
def test_a_field_json_leaves_empty_parses_as_the_xml_left_it_out(release, parse, expected) -> None:
    assert getattr(musicbrainz_tagger.Scraper(), parse)(release) == expected
