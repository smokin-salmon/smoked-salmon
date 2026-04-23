import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salmon.tagger.combine import combine_metadatas, combine_tracks


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
