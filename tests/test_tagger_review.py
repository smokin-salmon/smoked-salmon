import anyio

import salmon.tagger.review as review_module
from salmon.errors import InvalidMetadataError


def make_metadata() -> dict:
    return {
        "artists": [("Example Artist", "main")],
        "title": "Original Title",
        "group_year": "2004",
        "year": "2004",
        "edition_title": None,
        "label": "Old Label",
        "catno": "OLD-001",
        "rls_type": "Single",
        "genres": ["Pop"],
        "source": "WEB",
        "tracks": {"1": {"1": {"artists": [("Example Artist", "main")]}}},
    }


def test_review_metadata_defers_ai_fixable_label_errors_before_ai(monkeypatch) -> None:
    metadata = make_metadata()
    metadata["label"] = "X" * 81

    async def fake_prompt(*_args, **_kwargs):
        return "n"

    def fail_confirm(*_args, **_kwargs):
        raise AssertionError("click.confirm should not run before AI review for deferred label errors")

    def validator(_metadata):
        raise InvalidMetadataError("Label must be over 2 and under 80 characters.")

    monkeypatch.setattr(review_module, "_print_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(review_module.click, "prompt", fake_prompt)
    monkeypatch.setattr(review_module.click, "confirm", fail_confirm)

    result = anyio.run(review_module.review_metadata, metadata, validator, False)

    assert result is metadata


def test_review_metadata_still_prompts_for_label_errors_in_final_review(monkeypatch) -> None:
    metadata = make_metadata()
    metadata["label"] = "X" * 81
    confirm_calls: list[str] = []

    async def fake_prompt(*_args, **_kwargs):
        return "n"

    def fake_confirm(message, **_kwargs):
        confirm_calls.append(str(message))
        raise RuntimeError("confirm called")

    def validator(_metadata):
        raise InvalidMetadataError("Label must be over 2 and under 80 characters.")

    monkeypatch.setattr(review_module, "_print_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(review_module.click, "prompt", fake_prompt)
    monkeypatch.setattr(review_module.click, "confirm", fake_confirm)

    try:
        anyio.run(review_module.review_metadata, metadata, validator, True)
    except RuntimeError as exc:
        assert str(exc) == "confirm called"
    else:
        raise AssertionError("Expected final review to prompt for the invalid label")

    assert any("Label must be over 2 and under 80 characters." in call for call in confirm_calls)
