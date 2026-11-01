"""[metadata.corrections] artists: a name a store always gets wrong is replaced before the review (#791)."""

from copy import deepcopy
from typing import Any

import anyio
import pytest

import salmon.commands
from salmon import cfg
from salmon.commands import build_description
from salmon.config.validations import Corrections
from salmon.tagger.metadata import _combine_scraped


def _track(number: str, artists: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "track#": number,
        "disc#": "1",
        "tracktotal": "3",
        "disctotal": "1",
        "artists": artists,
        "title": f"Song {number}",
        "replay_gain": None,
        "peak": None,
        "isrc": None,
        "explicit": None,
        "format": None,
        "streamable": None,
    }


def _scraped(*tracks: list[tuple[str, str]]) -> dict[str, Any]:
    """A store's release, as a scraper returns it, with these artists on its tracks."""
    return {
        "artists": [],
        "title": "Album",
        "group_year": "2024",
        "year": "2024",
        "date": None,
        "edition_title": None,
        "label": None,
        "catno": None,
        "rls_type": "Album",
        "genres": ["Jazz"],
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
        "source": "WEB",
        "cover": None,
        "upc": None,
        "comment": None,
        "url": "https://www.qobuz.com/us-en/album/album/abc123",
        "urls": ["https://www.qobuz.com/us-en/album/album/abc123"],
        "tracks": {"1": {str(n): _track(str(n), artists) for n, artists in enumerate(tracks, 1)}},
    }


def _combined(*tracks: list[tuple[str, str]]) -> dict[str, Any]:
    """The metadata the review gets after picking that release: the files' tags carry the store's names."""
    scraped = _scraped(*tracks)
    return _combine_scraped(["Qobuz"], [scraped], deepcopy(scraped), None)


@pytest.fixture
def corrections(monkeypatch):
    def set_artists(artists: dict[str, str]) -> None:
        monkeypatch.setattr(cfg.metadata.corrections, "artists", artists)

    return set_artists


def test_a_corrected_artist_is_renamed_as_main_guest_and_remixer_in_the_tracks_and_the_release(corrections) -> None:
    corrections({"Ryuichi Sakamoto": "坂本龍一"})

    metadata = _combined(
        [("Ryuichi Sakamoto", "main")],
        [("Alva Noto", "main"), ("Ryuichi Sakamoto", "guest")],
        [("Alva Noto", "main"), ("Ryuichi Sakamoto", "remixer")],
    )

    tracks = metadata["tracks"]["1"]
    assert tracks["1"]["artists"] == [("坂本龍一", "main")]
    assert ("坂本龍一", "guest") in tracks["2"]["artists"]
    assert ("坂本龍一", "remixer") in tracks["3"]["artists"]
    assert ("坂本龍一", "main") in metadata["artists"]
    assert not any(name == "Ryuichi Sakamoto" for name, _ in metadata["artists"])


def test_a_correction_matches_in_any_case_and_past_outer_spaces(corrections) -> None:
    corrections({" Ryuichi Sakamoto": "坂本龍一"})

    metadata = _combined([("RYUICHI sakamoto  ", "main")])

    assert metadata["tracks"]["1"]["1"]["artists"] == [("坂本龍一", "main")]


def test_a_corrected_name_is_written_without_outer_spaces(monkeypatch) -> None:
    monkeypatch.setattr(cfg.metadata, "corrections", Corrections(artists={"Ryuichi Sakamoto": " 坂本龍一  "}))

    metadata = _combined([("Ryuichi Sakamoto", "main")])

    assert metadata["tracks"]["1"]["1"]["artists"] == [("坂本龍一", "main")]
    assert metadata["artists"] == [("坂本龍一", "main")]


def test_a_correction_does_not_match_a_name_with_other_accents(corrections) -> None:
    corrections({"Gael Horellou": "Another Name"})

    metadata = _combined([("Gaël Horellou", "main")])

    assert metadata["artists"] == [("Gaël Horellou", "main")]


def test_a_label_named_after_a_corrected_main_artist_is_self_released(corrections) -> None:
    corrections({"Ryuichi Sakamoto": "坂本龍一"})
    scraped = _scraped([("Ryuichi Sakamoto", "main")])
    scraped["label"] = "坂本龍一"

    metadata = _combine_scraped(["Qobuz"], [scraped], deepcopy(scraped), None)

    assert metadata["label"] == "Self-Released"


def test_descgen_writes_the_corrected_names_and_credits_a_main_artist_who_is_also_a_guest_once(
    monkeypatch, corrections
) -> None:
    # The cleanup of an artist credited as both main and guest must see the corrected names: descgen writes the
    # credits as they are, with no later merge of the same name.
    corrections({"Ryuichi Sakamoto": "坂本龍一"})
    scraped = _scraped([("Ryuichi Sakamoto", "main"), ("Alva Noto", "main"), ("坂本龍一", "guest")])

    async def fake_run_metadata(url, return_source_name=False):
        return scraped, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)

    description = anyio.run(build_description, ["https://www.qobuz.com/us-en/album/album/abc123"])

    assert "Ryuichi Sakamoto" not in description
    assert description.count("坂本龍一") == 1


def test_with_no_corrections_the_metadata_is_unchanged(corrections) -> None:
    corrections({})
    tracks = ([("Ryuichi Sakamoto", "main")], [("Ryuichi Sakamoto", "main"), ("Alva Noto", "guest")])

    metadata = _combined(*tracks)

    assert metadata["tracks"]["1"]["1"]["artists"] == [("Ryuichi Sakamoto", "main")]
    assert metadata["tracks"]["1"]["2"]["artists"] == [("Ryuichi Sakamoto", "main"), ("Alva Noto", "guest")]
    assert metadata["artists"] == [("Ryuichi Sakamoto", "main"), ("Alva Noto", "guest")]


@pytest.mark.parametrize("artists", [{"Ryuichi Sakamoto": " "}, {" ": "坂本龍一"}])
def test_a_correction_with_an_empty_name_is_refused(artists: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="empty name"):
        Corrections(artists=artists)


def test_an_artist_corrected_twice_in_two_cases_is_refused() -> None:
    # Both would match the same names, so one of them would be silently dropped.
    with pytest.raises(ValueError, match="twice"):
        Corrections(artists={"Ryuichi Sakamoto": "坂本龍一", "ryuichi sakamoto ": "Sakamoto"})
