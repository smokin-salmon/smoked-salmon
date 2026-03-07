import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salmon.tagger.combine import combine_metadatas


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
