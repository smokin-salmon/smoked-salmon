import pytest

from salmon import cfg
from salmon.common.strings import substitute_forbidden_characters


@pytest.fixture(autouse=True)
def _defaults(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "fullwidth_replacements", False)
    monkeypatch.setattr(cfg.upload.formatting, "colon_as_dash", True)
    monkeypatch.setattr(cfg.upload.formatting, "blacklisted_substitution", "_")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Tin Cup: The Movie", "Tin Cup - The Movie"),
        ("Tin Cup : The Movie", "Tin Cup - The Movie"),
        ("Tin Cup :  The Movie", "Tin Cup - The Movie"),
        ("12:34", "12_34"),
        ("Re:Stage", "Re_Stage"),
        ("Title:", "Title_"),
        ("a: b? c", "a - b_ c"),
    ],
)
def test_a_subtitle_colon_becomes_a_dash_and_any_other_colon_is_substituted(name: str, expected: str) -> None:
    assert substitute_forbidden_characters(name) == expected


def test_fullwidth_replacements_win_over_the_colon_rule(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "fullwidth_replacements", True)

    assert substitute_forbidden_characters("Tin Cup: The Movie") == "Tin Cup： The Movie"


def test_colon_as_dash_off_substitutes_every_colon(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "colon_as_dash", False)

    assert substitute_forbidden_characters("Tin Cup: The Movie 12:34") == "Tin Cup_ The Movie 12_34"
