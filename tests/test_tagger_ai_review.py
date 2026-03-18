import sys
from copy import deepcopy
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from salmon import cfg
from salmon.tagger import ai_review
from salmon.tagger.ai_review import (
    _ai_review_schema,
    _build_release_reference,
    _build_request_payload,
    _extract_progress_updates,
    _format_ai_progress,
    _style_ai_progress,
    apply_ai_metadata_result,
    build_ai_review_diff,
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


def make_review(**metadata_overrides) -> dict:
    metadata = make_metadata()
    review_metadata = {
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


def test_apply_ai_metadata_result_replaces_album_level_fields() -> None:
    metadata = make_metadata()
    review = make_review(
        label="New Label",
        year="2003",
        group_year="2003",
        genres=["Electronic", "Deep House", "Electronic"],
        urls=["https://official.example/release", "https://official.example/release"],
    )

    updated = apply_ai_metadata_result(metadata, review)

    assert updated["label"] == "New Label"
    assert updated["year"] == "2003"
    assert updated["group_year"] == "2003"
    assert updated["genres"] == ["Electronic", "Deep House"]
    assert updated["urls"] == ["https://official.example/release"]
    assert updated["tracks"]["1"]["2"]["title"] == "Track Two"
    assert metadata["label"] == "Old Label"


def test_apply_ai_metadata_result_can_clear_fields() -> None:
    metadata = make_metadata()
    review = make_review(label=None, catno=None, genres=[], urls=[])

    updated = apply_ai_metadata_result(metadata, review)

    assert updated["label"] is None
    assert updated["catno"] is None
    assert updated["genres"] == []
    assert updated["urls"] == []


def test_build_ai_review_diff_reports_only_actual_changes() -> None:
    metadata = make_metadata()
    review = make_review(
        label="New Label",
        year="2003",
        genres=["Electronic", "Deep House"],
        urls=metadata["urls"],
    )

    diff_lines = build_ai_review_diff(metadata, review)

    assert "label: Old Label -> New Label" in diff_lines
    assert "year: 2004 -> 2003" in diff_lines
    assert "genres: Electronic -> Electronic, Deep House" in diff_lines
    assert not any(line.startswith("urls:") for line in diff_lines)
    assert not any("track" in line for line in diff_lines)


def test_ai_review_schema_requires_every_metadata_key() -> None:
    metadata_schema = _ai_review_schema()["properties"]["metadata"]
    assert metadata_schema["required"] == [
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


def test_build_request_payload_requests_reasoning_summary_and_does_not_store() -> None:
    payload = _build_request_payload(make_metadata(), make_metadata(), None)

    assert payload["reasoning"]["summary"] == "auto"
    assert payload["store"] is False


def test_build_release_reference_keeps_only_identifying_fields() -> None:
    reference = _build_release_reference(make_metadata(), "https://example.com/release")

    assert reference == {
        "artists": [{"name": "Example Artist", "role": "main"}],
        "release_title_hint": "Original Title",
        "release_type_hint": "Album",
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "selected_source_url": "https://example.com/release",
    }


def test_build_request_payload_only_sends_release_reference() -> None:
    payload = _build_request_payload(make_metadata(), make_metadata(), None)
    prompt = payload["input"]

    assert "Release reference JSON:" in prompt
    assert "Current editable metadata JSON:" not in prompt
    assert "Editable metadata from local tags before Salmon combined sources:" not in prompt
    assert '"release_title_hint": "Original Title"' in prompt
    assert "Track One" not in prompt
    assert "Old Label" not in prompt


def test_format_ai_progress_compacts_without_clamping_text() -> None:
    formatted = _format_ai_progress("line one\nline two   " + ("x" * 220))

    assert "\n" not in formatted
    assert "line one line two" in formatted
    assert formatted.endswith("x" * 220)


def test_style_ai_progress_renders_markdown_bold_without_literal_asterisks() -> None:
    styled = _style_ai_progress("reasoning: **Verifying release-level evidence** right now")

    assert "**" not in styled
    assert "Verifying release-level evidence" in styled
    assert "\x1b[" in styled


def test_extract_progress_updates_reports_reasoning_and_web_search() -> None:
    payload = {
        "output": [
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "Comparing Bandcamp and MusicBrainz."},
                    {"type": "summary_text", "text": "Keeping the anchor page as primary evidence."},
                ],
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
                "action": {"type": "search", "query": '"Mouse and Banjo" "Dawn//Dust"'},
            },
            {
                "id": "ws_789",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "find_in_page", "url": "https://example.com/release"},
            },
            {
                "id": "ws_999",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "open_page"},
            },
        ]
    }

    lines, last_summary = _extract_progress_updates(payload, set(), None)

    assert any(line == "reasoning: Keeping the anchor page as primary evidence." for line in lines)
    assert any(line == 'web_search: completed | search | "Mouse and Banjo" "Dawn//Dust"' for line in lines)
    assert not any("web_search: searching" in line for line in lines)
    assert not any("find in page" in line for line in lines)
    assert not any(line.endswith("open page") for line in lines)
    assert last_summary == "Keeping the anchor page as primary evidence."


def test_review_metadata_with_ai_runs_after_manual_review_and_reopens_after_apply(monkeypatch) -> None:
    metadata = make_metadata()
    review = make_review(label="New Label")
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
    confirm_kwargs: dict[str, object] = {}
    original_enabled = cfg.upload.ai_review.enabled

    async def fake_manual_review(current_metadata, _validator):
        sequence.append("manual")
        return current_metadata

    def fake_confirm(*_args, **_kwargs):
        confirm_kwargs.update(_kwargs)
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
    assert confirm_kwargs["default"] is None


def test_review_metadata_with_ai_auto_keeps_when_no_suggestions(monkeypatch) -> None:
    metadata = make_metadata()
    review = make_review()
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
