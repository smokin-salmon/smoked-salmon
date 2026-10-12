import asyncio

from salmon import cfg
from salmon.tagger.sources.beatport import Scraper


def make_soup(*tracks: dict) -> dict:
    return {"tracks": list(tracks)}


def track(track_id: int, name: str, mix_name: str | None) -> dict:
    return {
        "id": track_id,
        "name": name,
        "mix_name": mix_name,
        "artists": [{"name": "An Artist"}],
        "remixers": [],
        "is_available_for_streaming": True,
        "isrc": None,
    }


def titles(soup: dict) -> list[str]:
    tracks = asyncio.run(Scraper().parse_tracks(soup))
    return [t["title"] for t in tracks["1"].values()]


def test_beatport_drops_original_mix_by_default() -> None:
    soup = make_soup(track(1, "Django", "Original Mix"), track(2, "Work", "Extended Mix"))

    assert titles(soup) == ["Django", "Work (Extended Mix)"]


def test_beatport_keeps_original_mix_when_useless_versions_are_kept(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "strip_useless_versions", False)
    soup = make_soup(track(1, "Django", "Original Mix"), track(2, "Work", "Extended Mix"))

    assert titles(soup) == ["Django (Original Mix)", "Work (Extended Mix)"]


def test_beatport_name_padded_with_a_space_gets_no_double_space() -> None:
    soup = make_soup(track(1, "A better place ", "Extended Mix"), track(2, "Lonely Drive ", "Original Mix"))

    assert titles(soup) == ["A better place (Extended Mix)", "Lonely Drive"]


def test_beatport_track_without_a_mix_name_keeps_its_name() -> None:
    assert titles(make_soup(track(1, "Django", None))) == ["Django"]
