import pytest

from salmon import cfg
from salmon.tagger.sources.apple_music import Scraper as AppleMusicScraper
from salmon.tagger.sources.qobuz import Scraper as QobuzScraper


def release(title: str) -> dict:
    tracks = {"1": {str(n): {"title": f"Track {n}"} for n in range(1, 5)}}
    return {"title": title, "rls_type": None, "tracks": tracks, "artists": [("An Artist", "main")]}


def title_and_type(title: str) -> tuple[str, str]:
    return AppleMusicScraper().determine_rls_type(release(title))


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Space EP", "Space"),
        ("Space E.P.", "Space"),
        ("Space - EP", "Space"),
        ("Space (EP)", "Space"),
        ("Space [EP]", "Space"),
        ("The EP Sessions Vol. 2", "The EP Sessions Vol. 2"),
        ("EP 1", "EP 1"),
        ("EP", "EP"),
    ],
)
def test_only_the_ep_ending_the_title_is_dropped(title: str, expected: str) -> None:
    assert title_and_type(title) == (expected, "EP")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Space EP", "Space EP"),
        ("Space E.P.", "Space E.P."),
        ("Space - EP", "Space"),
        ("Space (EP)", "Space"),
        ("The EP Sessions Vol. 2", "The EP Sessions Vol. 2"),
    ],
)
def test_ep_ending_the_title_is_kept_when_not_stripped(monkeypatch, title: str, expected: str) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "strip_ep_from_title", False)

    assert title_and_type(title) == (expected, "EP")


@pytest.mark.parametrize("strip", [True, False])
def test_qobuz_title_without_a_release_type_follows_the_setting(monkeypatch, strip: bool) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "strip_ep_from_title", strip)
    soups = [{"release_type": "", "title": "Space EP"}, {"release_type": "", "title": "Space - EP"}]

    assert [QobuzScraper().parse_release_type(soup) for soup in soups] == ["EP", "EP"]
    assert [soup["title"] for soup in soups] == ["Space" if strip else "Space EP", "Space"]
