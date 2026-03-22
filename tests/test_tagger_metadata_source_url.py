import asyncio
from copy import deepcopy

from salmon.sources.qobuz import QobuzBase
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


class DummyBandcampScraper:
    regex = type("Regex", (), {"match": staticmethod(lambda value: "/album/" in value)})

    async def scrape_release(self, url: str):
        data = deepcopy(make_release_data())
        data["title"] = "Bandcamp Title"
        data["urls"] = [url]
        return data


class DummyBandcampSource:
    Scraper = DummyBandcampScraper


class DummyQobuzScraper:
    regex = QobuzBase.regex

    async def scrape_release(self, url: str):
        data = deepcopy(make_release_data())
        data["title"] = "Qobuz Title"
        data["urls"] = [url]
        return data


class DummyQobuzSource:
    Scraper = DummyQobuzScraper


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


def test_qobuz_regex_matches_open_qobuz_urls() -> None:
    assert QobuzBase.regex.match("https://open.qobuz.com/album/0887396827479")


def test_select_choice_routes_open_qobuz_urls_to_qobuz_before_bandcamp(monkeypatch) -> None:
    def fail_prompt(*args, **kwargs):
        raise AssertionError("click.prompt should not run when a preferred source URL is provided")

    monkeypatch.setattr(metadata_mod.click, "prompt", fail_prompt)
    monkeypatch.setattr(
        metadata_mod,
        "METASOURCES",
        {
            "Qobuz": DummyQobuzSource,
            "Bandcamp": DummyBandcampSource,
        },
    )

    metadata, source_url = asyncio.run(
        metadata_mod._select_choice({}, make_release_data(), preferred_source_url="https://open.qobuz.com/album/0887396827479")
    )

    assert source_url == "https://open.qobuz.com/album/0887396827479"
    assert metadata["title"] == "Qobuz Title"
