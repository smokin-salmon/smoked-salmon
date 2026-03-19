import asyncio
from copy import deepcopy

from salmon.tagger import metadata as metadata_mod


def make_release_data() -> dict:
    return {
        "artists": [("Example Artist", "main")],
        "title": "Example Release",
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
                    "artists": [("Example Artist", "main")],
                    "title": "Track One",
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


class DummyScraper:
    regex = type("Regex", (), {"match": staticmethod(lambda value: value.startswith("https://example.com/album/"))})

    async def scrape_release(self, url: str):
        data = deepcopy(make_release_data())
        data["title"] = "Scraped Title"
        data["url"] = url
        data["urls"] = [url]
        return data


class DummySource:
    Scraper = DummyScraper


def test_select_choice_uses_preferred_source_url_without_prompt(monkeypatch) -> None:
    def fail_prompt(*args, **kwargs):
        raise AssertionError("click.prompt should not run when a preferred source URL is provided")

    monkeypatch.setattr(metadata_mod.click, "prompt", fail_prompt)
    monkeypatch.setattr(metadata_mod, "METASOURCES", {"Bandcamp": DummySource})

    metadata, source_url = asyncio.run(
        metadata_mod._select_choice({}, make_release_data(), preferred_source_url="https://example.com/album/release")
    )

    assert source_url == "https://example.com/album/release"
    assert metadata["title"] == "Scraped Title"
    assert metadata["urls"] == ["https://example.com/album/release"]
