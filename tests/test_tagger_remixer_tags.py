"""The remixers a store credits are written to the files' REMIXER tags, and never one only read from a title
(#803)."""

import asyncio
from copy import deepcopy
from typing import Any

import msgspec
import pytest
from helpers.tagger_combine import one_track
from helpers.tagger_metadata_corrections import _scraped
from helpers.tagger_multi_value_tags import (
    _metadata,
    _retag,
    _trackmeta,
    _write_flac,
    _write_m4a,
    _write_mp3,
)
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TPE4, TRCK

from salmon import cfg
from salmon.tagger.combine import combine_tracks
from salmon.tagger.metadata import _combine_scraped
from salmon.tagger.retagger import create_remixer_list
from salmon.tagger.sources import beatport, discogs, tidal
from salmon.tagger.sources.base import generate_artists

REMIXED = [("Dree Low", "main"), ("Owen", "remixer"), ("Jamm", "remixer")]


@pytest.fixture(autouse=True)
def _formatting(monkeypatch):
    monkeypatch.setattr(
        cfg.upload,
        "formatting",
        msgspec.structs.replace(cfg.upload.formatting, guests_in_track_title=False, various_artist_threshold=4),
    )


def _release(artists: list[tuple[str, str]], credited: list[str]) -> dict[str, Any]:
    return _metadata({**_trackmeta(artists), "credited_remixers": credited})


def _remixer_changes(track_changes: dict) -> list:
    return [change for changes in track_changes.values() for change in changes if change.tag == "remixer"]


def test_credited_remixers_are_written_to_a_flac_one_value_each(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", TRACKNUMBER="1", TITLE="Song")

    _retag(tmp_path, _release(REMIXED, ["Owen", "Jamm"]))

    assert FLAC(tmp_path / "01.flac").get("remixer") == ["Owen", "Jamm"]


def test_credited_remixers_replace_an_mp3s_tpe4_with_one_frame(tmp_path) -> None:
    # TPE4, as a transcode writes a FLAC's REMIXER.
    _write_mp3(tmp_path / "01.mp3", TRCK(encoding=3, text="1"), TPE4(encoding=3, text="Someone Else"))

    _retag(tmp_path, _release(REMIXED, ["Owen", "Jamm"]))

    assert [frame.text for frame in ID3(tmp_path / "01.mp3").getall("TPE4")] == [["Owen", "Jamm"]]


def test_a_remixer_only_read_from_the_title_is_not_written_and_the_files_own_stays(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", TRACKNUMBER="1", TITLE="Song", REMIXER="Someone Else")

    _, track_changes = _retag(tmp_path, _release(REMIXED, []))

    assert _remixer_changes(track_changes) == []
    assert FLAC(tmp_path / "01.flac").get("remixer") == ["Someone Else"]


def test_no_change_is_proposed_for_remixers_the_file_already_holds(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", TRACKNUMBER="1", TITLE="Song", REMIXER=["Owen", "Jamm"])

    _, track_changes = _retag(tmp_path, _release(REMIXED, ["Owen", "Jamm"]))

    assert _remixer_changes(track_changes) == []


def test_an_m4a_gets_no_remixer(tmp_path) -> None:
    _write_m4a(tmp_path / "01.m4a", trkn=[(1, 0)])

    _, track_changes = _retag(tmp_path, _release(REMIXED, ["Owen", "Jamm"]))

    assert _remixer_changes(track_changes) == []


def test_beatport_credits_its_remixers_field() -> None:
    track = {
        "id": 1,
        "name": "Song",
        "mix_name": "Owen Remix",
        "artists": [{"name": "Dree Low"}],
        "remixers": [{"name": "Owen"}],
        "is_available_for_streaming": True,
        "isrc": None,
    }

    tracks = asyncio.run(beatport.Scraper().parse_tracks({"tracks": [track]}))

    assert tracks["1"]["1"]["credited_remixers"] == ["Owen"]


def test_discogs_credits_its_remix_credits_under_both_names() -> None:
    track = {
        "type_": "track",
        "position": "1",
        "title": "Song",
        "artists": [{"name": "Dree Low", "join": ""}],
        "extraartists": [
            {"name": "Trouble Men", "anv": "DJ Trouble", "role": "Remix"},
            {"name": "Jamm", "role": "Producer"},
        ],
    }

    tracks = asyncio.run(discogs.Scraper().parse_tracks({"tracklist": [track], "artists": []}))

    assert tracks["1"]["1"]["credited_remixers"] == ["Trouble Men", "DJ Trouble"]


def test_tidal_credits_a_remixer_it_lists_and_not_one_only_in_the_version() -> None:
    # Owen is among Tidal's artists for the track; Jamm is only in the version.
    _, credited = tidal.Scraper().parse_artists(
        [{"name": "Dree Low"}, {"name": "Owen"}], "Song (Owen Remix)", "Jamm Remix", [{"name": "Dree Low"}]
    )

    assert credited == ["Owen"]


def test_tidal_credits_a_featured_guest_who_also_remixed_the_track() -> None:
    artists, credited = tidal.Scraper().parse_artists(
        [{"name": "Dree Low"}, {"name": "Owen"}], "Song (feat. Owen)", "Owen Remix", [{"name": "Dree Low"}]
    )

    assert {("Owen", "guest"), ("Owen", "remixer")} <= set(artists)
    assert credited == ["Owen"]


def _one_track(artists: list[tuple[str, str]], credited: list[str]) -> dict[str, Any]:
    """The track as a scrape leaves it, after generate_artists."""
    tracks = {"1": {"1": {"artists": artists, "credited_remixers": credited, "title": "Song"}}}
    return generate_artists(tracks)[1]["1"]["1"]


def test_a_featured_guest_who_remixed_the_track_is_written_as_remixer() -> None:
    artists, credited = tidal.Scraper().parse_artists(
        [{"name": "Dree Low"}, {"name": "Owen"}], "Song (feat. Owen)", "Owen Remix", [{"name": "Dree Low"}]
    )

    assert create_remixer_list(_one_track(artists, credited)) == ["Owen"]


def test_an_artists_own_remix_is_written_as_remixer() -> None:
    # Beatport credits the main artist as the remixer of their own track.
    track = {
        "id": 1,
        "name": "Song",
        "mix_name": "Dree Low Remix",
        "artists": [{"name": "Dree Low"}],
        "remixers": [{"name": "Dree Low"}],
        "is_available_for_streaming": True,
        "isrc": None,
    }
    scraped = asyncio.run(beatport.Scraper().parse_tracks({"tracks": [track]}))["1"]["1"]

    track = _one_track(scraped["artists"], scraped["credited_remixers"])

    assert ("Dree Low", "main") in track["artists"]
    assert create_remixer_list(track) == ["Dree Low"]


def test_one_name_as_main_and_guest_is_still_one_credit() -> None:
    assert _one_track([("Dree Low", "main"), ("Dree Low", "guest")], [])["artists"] == [("Dree Low", "main")]


def test_one_name_as_main_and_arranger_keeps_both_credits() -> None:
    track = _one_track([("Dree Low", "main"), ("Dree Low", "arranger")], [])

    assert track["artists"] == [("Dree Low", "main"), ("Dree Low", "arranger")]


def test_tidal_tells_names_in_another_script_apart() -> None:
    # Compared as "", only the first name in another script was kept, and taken for the "(X Remix)" remixer.
    artists, credited = tidal.Scraper().parse_artists(
        [{"name": "宇多田ヒカル"}, {"name": "坂本龍一"}], "Song (坂本龍一 Remix)", None, [{"name": "宇多田ヒカル"}]
    )

    assert artists == [("宇多田ヒカル", "main"), ("坂本龍一", "remixer")]
    assert credited == ["坂本龍一"]


def test_the_merge_keeps_a_credit_the_files_already_name_as_remixer() -> None:
    # The files already name Owen as remixer, so the store's credit is not added again, but it still counts.
    base = one_track([("Dree Low", "main"), ("Owen", "remixer")], "Song (Owen Remix)")
    meta = one_track([("Dree Low", "main"), ("Owen", "remixer")], "Song (Owen Remix)")
    meta["1"]["1"]["credited_remixers"] = ["Owen"]

    track = combine_tracks(deepcopy(base), meta, True)["1"]["1"]

    assert track["artists"].count(("Owen", "remixer")) == 1
    assert create_remixer_list(track) == ["Owen"]


def test_the_merge_does_not_credit_a_remixer_read_from_the_title() -> None:
    base = one_track([("Dree Low", "main")], "Song")
    meta = one_track([("Dree Low", "main")], "Song (Owen Remix)")

    track = combine_tracks(deepcopy(base), meta, True)["1"]["1"]

    assert ("Owen", "remixer") in track["artists"]
    assert create_remixer_list(track) == []


def test_a_credited_remixer_reaches_the_retag_corrected_and_as_the_files_spell_it(monkeypatch) -> None:
    # The files spell the remixer "gaël x", the store "Gaël X"; a correction renames the other remixer.
    monkeypatch.setattr(cfg.metadata.corrections, "artists", {"Owen": "Owen Pallett"})
    files = _scraped([("Dree Low", "main"), ("gaël x", "remixer")])
    scraped = _scraped([("Dree Low", "main"), ("Gaël X", "remixer"), ("Owen", "remixer")])
    scraped["tracks"]["1"]["1"]["credited_remixers"] = ["Gaël X", "Owen"]

    metadata = _combine_scraped(["Qobuz"], [scraped], files, None)

    assert create_remixer_list(metadata["tracks"]["1"]["1"]) == ["gaël x", "Owen Pallett"]
