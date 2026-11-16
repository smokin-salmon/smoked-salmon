"""Helpers of tests/test_tagger_combine.py that other test modules use too."""

import pytest

from salmon.tagger.sources import METASOURCES


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


def one_track(artists: list[tuple[str, str]], title: str) -> dict:
    track = make_tracks({"1": ["1"]}, isrc_prefix=None)["1"]["1"]
    return {"1": {"1": {**track, "artists": artists, "title": title}}}


def stating(meta: dict, *guessed: tuple[str, str]) -> dict:
    """The release as a store that states its credits hands it to the merge: its first track's credits stated, but
    for `guessed`."""
    meta["tracks"]["1"]["1"]["assumed_artists"] = list(guessed)
    return meta


def stores_stating(monkeypatch: pytest.MonkeyPatch, stating: tuple[str, ...], assuming: tuple[str, ...]) -> None:
    """Make the `stating` stores state their credits and the `assuming` ones assume theirs, whatever their scrapers
    do today."""
    for names, states in ((stating, True), (assuming, False)):
        for name in names:
            monkeypatch.setattr(METASOURCES[name].Scraper, "states_credits", states)
