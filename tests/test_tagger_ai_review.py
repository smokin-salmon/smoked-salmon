import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salmon.tagger.ai_review import apply_ai_metadata_patch, build_ai_review_diff


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


def test_apply_ai_metadata_patch_updates_allowed_fields_and_track_titles() -> None:
    metadata = make_metadata()
    review = {
        "summary": "Updated metadata",
        "patch": {
            "label": "New Label",
            "year": "2003",
            "group_year": "2003",
            "genres": ["Electronic", "Deep House", "Electronic"],
            "urls": ["https://official.example/release", "https://official.example/release"],
        },
        "track_title_changes": [{"disc_number": "1", "track_number": "2", "title": "Track Two (Extended Mix)"}],
        "citations": [],
    }

    updated = apply_ai_metadata_patch(metadata, review)

    assert updated["label"] == "New Label"
    assert updated["year"] == "2003"
    assert updated["group_year"] == "2003"
    assert updated["genres"] == ["Electronic", "Deep House"]
    assert updated["urls"] == ["https://official.example/release"]
    assert updated["tracks"]["1"]["2"]["title"] == "Track Two (Extended Mix)"
    assert metadata["label"] == "Old Label"


def test_build_ai_review_diff_reports_only_actual_changes() -> None:
    metadata = make_metadata()
    review = {
        "summary": "Updated metadata",
        "patch": {
            "label": "New Label",
            "year": "2003",
            "genres": ["Electronic", "Deep House"],
            "urls": metadata["urls"],
        },
        "track_title_changes": [{"disc_number": "1", "track_number": "2", "title": "Track Two (Extended Mix)"}],
        "citations": [],
    }

    diff_lines = build_ai_review_diff(metadata, review)

    assert "label: Old Label -> New Label" in diff_lines
    assert "year: 2004 -> 2003" in diff_lines
    assert "genres: Electronic -> Electronic, Deep House" in diff_lines
    assert "track 1-2 title: Track Two -> Track Two (Extended Mix)" in diff_lines
    assert not any(line.startswith("urls:") for line in diff_lines)


def test_apply_ai_metadata_patch_rejects_missing_track_reference() -> None:
    metadata = make_metadata()
    review = {
        "summary": "Updated metadata",
        "patch": {},
        "track_title_changes": [{"disc_number": "9", "track_number": "99", "title": "Ghost Track"}],
        "citations": [],
    }

    try:
        apply_ai_metadata_patch(deepcopy(metadata), review)
    except ValueError as exc:
        assert "missing track reference" in str(exc)
    else:
        raise AssertionError("Expected AI patch with missing track reference to fail")
