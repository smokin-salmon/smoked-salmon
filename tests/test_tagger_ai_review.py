import sys
from copy import deepcopy
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salmon import cfg
from salmon.tagger import ai_review
from salmon.tagger.ai_review import (
    _ai_review_schema,
    _build_editable_metadata_snapshot,
    _build_release_reference,
    _build_request_payload,
    _extract_progress_updates,
    _format_ai_progress,
    _style_ai_progress,
    apply_ai_metadata_patch,
    build_ai_review_diff,
    summarize_ai_review_citation_titles,
)


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


def test_ai_review_schema_requires_every_patch_key() -> None:
    patch_schema = _ai_review_schema()["properties"]["patch"]
    assert patch_schema["required"] == [
        "title",
        "group_year",
        "year",
        "edition_title",
        "label",
        "catno",
        "upc",
        "genres",
        "urls",
    ]


def test_build_request_payload_requests_reasoning_summary() -> None:
    payload = _build_request_payload(make_metadata(), make_metadata(), None)

    assert payload["reasoning"]["summary"] == "auto"


def test_build_release_reference_keeps_only_identifying_fields() -> None:
    reference = _build_release_reference(make_metadata(), "https://example.com/release")

    assert reference == {
        "artists": [{"name": "Example Artist", "role": "main"}],
        "release_type": "Album",
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
        "track_count": 2,
        "selected_source_url": "https://example.com/release",
    }


def test_build_editable_metadata_snapshot_keeps_only_editable_fields_and_track_titles() -> None:
    snapshot = _build_editable_metadata_snapshot(make_metadata())

    assert snapshot == {
        "title": "Original Title",
        "group_year": "2004",
        "year": "2004",
        "edition_title": None,
        "label": "Old Label",
        "catno": "OLD-001",
        "upc": None,
        "genres": ["Electronic"],
        "urls": ["https://old.example/release"],
        "track_titles": [
            {"disc_number": "1", "track_number": "1", "title": "Track One"},
            {"disc_number": "1", "track_number": "2", "title": "Track Two"},
        ],
    }


def test_format_ai_progress_compacts_without_clamping_text() -> None:
    formatted = _format_ai_progress("line one\nline two   " + ("x" * 220))

    assert "\n" not in formatted
    assert "line one line two" in formatted
    assert formatted.endswith("x" * 220)


def test_style_ai_progress_renders_markdown_bold_without_literal_asterisks() -> None:
    styled = _style_ai_progress("reasoning: **Verifying track details** right now")

    assert "**" not in styled
    assert "Verifying track details" in styled
    assert "\x1b[" in styled


def test_summarize_ai_review_citation_titles_returns_display_text() -> None:
    summary = summarize_ai_review_citation_titles(
        [
            "- Dawn//Dust | Mouse and Banjo | Bandcamp (summary, genres): https://example.com/a",
            '- Release "Dawn//Dust" by Mouse and Banjo - MusicBrainz (summary): https://example.com/b',
        ]
    )

    assert summary == (
        'Dawn//Dust | Mouse and Banjo | Bandcamp (summary, genres), '
        'Release "Dawn//Dust" by Mouse and Banjo - MusicBrainz (summary)'
    )


def test_extract_progress_updates_reports_reasoning_and_web_search() -> None:
    payload = {
        "output": [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Comparing Bandcamp and MusicBrainz."}],
            },
            {
                "id": "ws_123",
                "type": "web_search_call",
                "status": "searching",
                "action": {"type": "search", "query": "Mouse and Banjo Dawn Dust label"},
            },
            {
                "id": "ws_456",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "\"Mouse and Banjo\" \"Dawn//Dust\""},
            },
        ]
    }

    lines, last_summary = _extract_progress_updates(payload, set(), None)

    assert any(line == "reasoning: Comparing Bandcamp and MusicBrainz." for line in lines)
    assert any(line == 'web_search: completed | search | "Mouse and Banjo" "Dawn//Dust"' for line in lines)
    assert not any("web_search: searching" in line for line in lines)
    assert last_summary == "Comparing Bandcamp and MusicBrainz."


def test_review_metadata_with_ai_runs_after_manual_review_and_reopens_after_apply(monkeypatch) -> None:
    metadata = make_metadata()
    review = {
        "summary": "Updated metadata",
        "patch": {"label": "New Label"},
        "track_title_changes": [],
        "citations": [],
    }
    sequence: list[str] = []
    original_enabled = cfg.upload.ai_review.enabled

    async def fake_manual_review(current_metadata, _validator):
        sequence.append(f"manual:{current_metadata['label']}")
        return current_metadata

    async def fake_request_ai_review(*_args, **_kwargs):
        sequence.append("ai")
        return review, "response-1"

    async def fake_prompt(*_args, **_kwargs):
        return "a"

    def fake_confirm(*_args, **_kwargs):
        sequence.append("confirm")
        return True

    try:
        cfg.upload.ai_review.enabled = True
        monkeypatch.setattr(ai_review, "_request_ai_review", fake_request_ai_review)
        monkeypatch.setattr(ai_review.click, "prompt", fake_prompt)
        monkeypatch.setattr(ai_review.click, "confirm", fake_confirm)

        result = anyio.run(
            ai_review.review_metadata_with_ai,
            metadata,
            metadata,
            None,
            lambda current_metadata: current_metadata,
            fake_manual_review,
        )
    finally:
        cfg.upload.ai_review.enabled = original_enabled

    assert result["label"] == "New Label"
    assert sequence == ["manual:Old Label", "confirm", "ai", "manual:New Label"]


def test_review_metadata_with_ai_skips_ai_when_user_declines(monkeypatch) -> None:
    metadata = make_metadata()
    sequence: list[str] = []
    original_enabled = cfg.upload.ai_review.enabled

    async def fake_manual_review(current_metadata, _validator):
        sequence.append("manual")
        return current_metadata

    def fake_confirm(*_args, **_kwargs):
        sequence.append("confirm")
        return False

    try:
        cfg.upload.ai_review.enabled = True
        monkeypatch.setattr(ai_review.click, "confirm", fake_confirm)

        result = anyio.run(
            ai_review.review_metadata_with_ai,
            metadata,
            metadata,
            None,
            lambda current_metadata: current_metadata,
            fake_manual_review,
        )
    finally:
        cfg.upload.ai_review.enabled = original_enabled

    assert result["label"] == "Old Label"
    assert sequence == ["manual", "confirm"]


def test_review_metadata_with_ai_auto_keeps_when_no_suggestions(monkeypatch) -> None:
    metadata = make_metadata()
    review = {
        "summary": "No strongly supported corrections found.",
        "patch": {
            "title": None,
            "group_year": None,
            "year": None,
            "edition_title": None,
            "label": None,
            "catno": None,
            "upc": None,
            "genres": [],
            "urls": [],
        },
        "track_title_changes": [],
        "citations": [
            {
                "title": "Dawn//Dust | Mouse and Banjo",
                "url": "https://mouseandbanjo.bandcamp.com/album/dawn-dust",
                "supports": ["summary", "title", "year"],
            }
        ],
    }
    sequence: list[str] = []
    original_enabled = cfg.upload.ai_review.enabled

    async def fake_manual_review(current_metadata, _validator):
        sequence.append("manual")
        return current_metadata

    async def fake_request_ai_review(*_args, **_kwargs):
        sequence.append("ai")
        return review, "response-1"

    def fake_confirm(*_args, **_kwargs):
        sequence.append("confirm")
        return True

    async def fake_prompt(*_args, **_kwargs):
        raise AssertionError("Prompt should not be shown when AI has no suggestions")

    try:
        cfg.upload.ai_review.enabled = True
        monkeypatch.setattr(ai_review, "_request_ai_review", fake_request_ai_review)
        monkeypatch.setattr(ai_review.click, "confirm", fake_confirm)
        monkeypatch.setattr(ai_review.click, "prompt", fake_prompt)

        result = anyio.run(
            ai_review.review_metadata_with_ai,
            metadata,
            metadata,
            None,
            lambda current_metadata: current_metadata,
            fake_manual_review,
        )
    finally:
        cfg.upload.ai_review.enabled = original_enabled

    assert result["label"] == "Old Label"
    assert sequence == ["manual", "confirm", "ai"]
