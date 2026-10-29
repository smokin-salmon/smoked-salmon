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


def test_edit_genres_keeps_existing_when_the_edit_normalizes_to_nothing(monkeypatch) -> None:
    metadata = make_metadata()
    monkeypatch.setattr(review_module.click, "edit", lambda *_a, **_k: "///\n,,,")
    monkeypatch.setattr(review_module.click, "secho", lambda *_a, **_k: None)
    anyio.run(review_module._edit_genres, metadata)
    assert metadata["genres"] == ["Pop"]


def test_edit_genres_standardizes_what_the_editor_returns(monkeypatch) -> None:
    metadata = make_metadata()
    monkeypatch.setattr(review_module.click, "edit", lambda *_a, **_k: "Dance / Pop\nDrum & Bass")
    anyio.run(review_module._edit_genres, metadata)
    assert sorted(metadata["genres"]) == ["Dance", "Drum & Bass", "Pop"]


def _run_review(monkeypatch, answers, editor=None):
    """Answer the edit menu from `answers`; `editor` stands in for the text editor."""
    replies = iter(answers)
    printed: list[str] = []

    async def fake_prompt(*_args, **_kwargs):
        return next(replies)

    monkeypatch.setattr(review_module, "_print_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(review_module.click, "prompt", fake_prompt)
    monkeypatch.setattr(review_module.click, "secho", lambda message, **_kwargs: printed.append(str(message)))
    if editor is not None:
        monkeypatch.setattr(review_module.click, "edit", lambda text, **_kwargs: editor(text))
    metadata = make_metadata()
    anyio.run(review_module.review_metadata, metadata, lambda _metadata: None, False)
    return metadata, printed


def test_typing_one_for_aliases_says_the_key_is_the_letter_l(monkeypatch) -> None:
    _metadata, printed = _run_review(monkeypatch, ["1", "n"])

    assert any("letter L" in line for line in printed)


def test_the_alias_editor_explains_its_syntax_and_splits_an_artist(monkeypatch) -> None:
    shown: list[str] = []

    def editor(text):
        shown.append(text)
        return text + "Example Artist --> Gorgon City\nExample Artist --> Jem Cooke\n"

    metadata, _printed = _run_review(monkeypatch, ["l", "n"], editor)

    assert "existing name --> new name" in shown[0]
    assert "README" not in shown[0]
    assert metadata["artists"] == [("Gorgon City", "main"), ("Jem Cooke", "main")]
    assert metadata["tracks"]["1"]["1"]["artists"] == [("Gorgon City", "main"), ("Jem Cooke", "main")]


def test_the_artist_editor_points_to_aliases_for_renames(monkeypatch) -> None:
    _metadata, printed = _run_review(monkeypatch, ["a", "n"], lambda text: text)

    assert any("a[L]iases" in line for line in printed)


def _tracks_metadata() -> dict:
    metadata = make_metadata()
    metadata["tracks"] = {
        "1": {
            "1": {"title": "One", "artists": [("A", "main")], "disc#": "1", "track#": "1"},
            "2": {"title": "Two", "artists": [("A", "main")], "disc#": "1", "track#": "2"},
        }
    }
    return metadata


def _edit_tracks_with(monkeypatch, metadata, edits):
    """Run `_edit_tracks`, the editor returning each of `edits` in turn (given the text it was shown)."""
    shown: list[str] = []
    confirms: list[str] = []
    replies = iter(edits)

    async def fake_edit(text, **_kwargs):
        shown.append(text)
        return next(replies)(text)

    async def fake_confirm(message, **_kwargs):
        confirms.append(str(message))
        return True

    monkeypatch.setattr(review_module.interaction, "edit", fake_edit)
    monkeypatch.setattr(review_module.interaction, "confirm", fake_confirm)
    anyio.run(review_module._edit_tracks, metadata)
    return shown, confirms


def test_edit_tracks_applies_a_valid_edit(monkeypatch) -> None:
    metadata = _tracks_metadata()
    _edit_tracks_with(monkeypatch, metadata, [lambda text: text.replace("Title: One", "Title: Uno")])
    assert metadata["tracks"]["1"]["1"]["title"] == "Uno"
    assert metadata["tracks"]["1"]["2"]["title"] == "Two"


def test_edit_tracks_refuses_a_changed_track_number_and_reopens_the_edited_text(monkeypatch) -> None:
    metadata = _tracks_metadata()

    def bad(text):
        return text.replace("Disc 1 Track 1", "Disc 1 Track S").replace("Title: Two", "Title: Deux")

    def fixed(text):
        return text.replace("Track S", "Track 1")

    shown, confirms = _edit_tracks_with(monkeypatch, metadata, [bad, fixed])

    assert len(confirms) == 1
    assert '"Disc 1 Track S" is not a track of this release' in confirms[0]
    assert "can't be changed, only the titles and artists under them" in confirms[0]
    assert "ValueError" not in confirms[0] and "<class" not in confirms[0]
    assert "Title: Deux" in shown[1] and "Track S" in shown[1]
    assert metadata["tracks"]["1"]["2"]["title"] == "Deux"  # applied once the retry is valid


def test_edit_tracks_applies_nothing_when_a_later_line_is_broken(monkeypatch) -> None:
    metadata = _tracks_metadata()

    def broken(text):
        return text.replace("Title: One", "Title: Uno").replace("Title: Two", "Two")

    _edit_tracks_with(monkeypatch, metadata, [broken, lambda _text: ""])

    assert metadata["tracks"]["1"]["1"]["title"] == "One"
    assert metadata["tracks"]["1"]["2"]["title"] == "Two"


def test_edit_tracks_refuses_a_deleted_artists_line_and_changes_nothing(monkeypatch) -> None:
    metadata = _tracks_metadata()

    def broken(text):
        return text.replace("Title: One", "Title: Uno").replace("Artists:\n> A (main)", "> A (main)", 1)

    _shown, confirms = _edit_tracks_with(monkeypatch, metadata, [broken, lambda _text: ""])

    assert len(confirms) == 1 and 'is not the "Artists:" line' in confirms[0]
    assert metadata["tracks"]["1"]["1"]["title"] == "One"
    assert metadata["tracks"]["1"]["1"]["artists"] == [("A", "main")]


def test_edit_tracks_refuses_a_track_named_twice_and_changes_nothing(monkeypatch) -> None:
    metadata = _tracks_metadata()

    def broken(text):
        return text.replace("Disc 1 Track 1", "Disc 1 Track 2").replace("Title: One", "Title: Uno")

    _shown, confirms = _edit_tracks_with(monkeypatch, metadata, [broken, lambda _text: ""])

    assert len(confirms) == 1 and '"Disc 1 Track 2" is named twice' in confirms[0]
    assert metadata["tracks"]["1"]["1"]["title"] == "One"
    assert metadata["tracks"]["1"]["2"]["title"] == "Two"
