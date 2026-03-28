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


def make_choice_source(source_name: str):
    source_slug = source_name.lower().replace(" ", "-")

    class DummySearcher:
        @staticmethod
        def format_url(rls_id: str) -> str:
            return f"https://{source_slug}.example/release/{rls_id}"

    class ChoiceScraper:
        regex = type("Regex", (), {"match": staticmethod(lambda _value: False)})

        async def scrape_release(self, url: str):
            data = deepcopy(make_release_data())
            data["url"] = url
            data["urls"] = [url]
            return data

        async def scrape_release_from_id(self, rls_id: str):
            return await self.scrape_release(DummySearcher.format_url(rls_id))

    return type("ChoiceSource", (), {"Searcher": DummySearcher, "Scraper": ChoiceScraper})


def test_select_choice_accepts_comma_separated_numeric_choices_and_preserves_url_order(monkeypatch) -> None:
    async def fake_prompt(*args, **kwargs):
        return "02, 06, 08, 09, 11"

    choice_sources = {
        "Apple Music": make_choice_source("Apple Music"),
        "Beatport": make_choice_source("Beatport"),
        "Qobuz": make_choice_source("Qobuz"),
        "Tidal": make_choice_source("Tidal"),
        "Deezer": make_choice_source("Deezer"),
    }
    choices = {
        2: ("Apple Music", "1664092970"),
        6: ("Beatport", "5485275"),
        8: ("Qobuz", "bidmhfep1iyya"),
        9: ("Tidal", "270638706"),
        11: ("Deezer", "394884007"),
    }
    expected_urls = [
        "https://apple-music.example/release/1664092970",
        "https://beatport.example/release/5485275",
        "https://qobuz.example/release/bidmhfep1iyya",
        "https://tidal.example/release/270638706",
        "https://deezer.example/release/394884007",
    ]

    def fake_combine(*metadatas, base=None, source_url=None):
        assert base is not None
        combined = deepcopy(base)
        combined["urls"] = [md["url"] for _source, md in reversed(metadatas)]
        return combined

    monkeypatch.setattr(metadata_mod.click, "prompt", fake_prompt)
    monkeypatch.setattr(metadata_mod, "METASOURCES", choice_sources)
    monkeypatch.setattr(metadata_mod, "SEARCHSOURCES", choice_sources)
    monkeypatch.setattr(metadata_mod, "combine_metadatas", fake_combine)
    monkeypatch.setattr(metadata_mod, "clean_metadata", lambda metadata: metadata)
    monkeypatch.setattr(
        metadata_mod,
        "generate_artists",
        lambda tracks: ([("Example Artist", "main")], tracks),
    )

    metadata, source_url = asyncio.run(metadata_mod._select_choice(choices, make_release_data()))

    assert source_url is None
    assert metadata["urls"] == expected_urls
