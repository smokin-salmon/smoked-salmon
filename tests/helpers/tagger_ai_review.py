"""Helpers of tests/test_tagger_ai_review.py that other test modules use too."""

from copy import deepcopy


def make_metadata() -> dict:
    return {
        "artists": [("Example Artist", "main")],
        "title": "Original Title",
        "group_year": "2004",
        "year": "2004",
        "date": None,
        "edition_title": None,
        "label": "Old Label",
        "catno": "OLD-001",
        "rls_type": "Album",
        "genres": ["Electronic"],
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
        "source": "WEB",
        "cover": None,
        "upc": None,
        "comment": None,
        "urls": ["https://old.example/release"],
        "tracks": {
            "1": {
                "1": {
                    "track#": "1",
                    "disc#": "1",
                    "tracktotal": "2",
                    "disctotal": "1",
                    "artists": [("Example Artist", "main")],
                    "title": "Track One",
                    "replay_gain": None,
                    "peak": None,
                    "isrc": None,
                    "explicit": None,
                    "format": None,
                    "streamable": None,
                },
                "2": {
                    "track#": "2",
                    "disc#": "1",
                    "tracktotal": "2",
                    "disctotal": "1",
                    "artists": [("Example Artist", "main")],
                    "title": "Track Two",
                    "replay_gain": None,
                    "peak": None,
                    "isrc": None,
                    "explicit": None,
                    "format": None,
                    "streamable": None,
                },
            }
        },
    }


def make_review(**metadata_overrides) -> dict:
    metadata = make_metadata()
    review_metadata = {
        "artists": [{"name": artist_name, "role": artist_role} for artist_name, artist_role in metadata["artists"]],
        "title": metadata["title"],
        "group_year": metadata["group_year"],
        "year": metadata["year"],
        "edition_title": metadata["edition_title"],
        "label": metadata["label"],
        "catno": metadata["catno"],
        "upc": metadata["upc"],
        "genres": deepcopy(metadata["genres"]),
        "urls": deepcopy(metadata["urls"]),
    }
    review_metadata.update(metadata_overrides)
    return {
        "summary": "Updated metadata",
        "metadata": review_metadata,
        "citations": [],
    }
