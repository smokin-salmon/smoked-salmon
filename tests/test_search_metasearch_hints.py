"""The files' hints in the store search: MusicBrainz searches by field, once, every other store as before (#1178)."""

from functools import partial
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
from helpers.webui_search import FOUND, _fake_stores

from salmon.errors import ScrapeError
from salmon.search import run_metasearch
from salmon.search.hints import SearchHints, search_hints
from salmon.tagger import metadata as metadata_mod

HINTS = SearchHints("The Artist", "The Album", 10, "2020")
SEARCHSTRS = ["a1 the album", "a2 the album"]


def _queries(searches: list[tuple[str, str, int, bool]], store: str) -> list[str]:
    return [query for name, query, *_ in searches if name == store]


def test_with_hints_musicbrainz_gets_one_fielded_search(monkeypatch) -> None:
    searches = _fake_stores(monkeypatch)
    anyio.run(partial(run_metasearch, SEARCHSTRS, hints=HINTS))
    assert _queries(searches, "MusicBrainz") == ["artist:(the artist) release:(the album) tracks:(10) date:(2020)"]


def test_with_hints_every_other_store_gets_every_searchstr(monkeypatch) -> None:
    searches = _fake_stores(monkeypatch)
    anyio.run(partial(run_metasearch, SEARCHSTRS, hints=HINTS))
    others = {name for name, *_ in searches} - {"MusicBrainz"}
    assert others
    for store in others:
        assert _queries(searches, store) == SEARCHSTRS, store


def test_without_hints_musicbrainz_gets_the_searchstrs(monkeypatch) -> None:
    searches = _fake_stores(monkeypatch)
    anyio.run(partial(run_metasearch, SEARCHSTRS))
    assert _queries(searches, "MusicBrainz") == SEARCHSTRS


def test_a_failing_musicbrainz_with_hints_leaves_the_other_stores(monkeypatch, capsys) -> None:
    _fake_stores(monkeypatch, {**FOUND, "MusicBrainz": ScrapeError("MusicBrainz is down")})
    results = anyio.run(partial(run_metasearch, SEARCHSTRS, hints=HINTS))
    assert "MusicBrainz" not in results
    assert results["Deezer"]
    assert click.unstyle(capsys.readouterr().out).count("MusicBrainz is down") == 1


def test_get_metadata_passes_the_files_hints(monkeypatch) -> None:
    tags = {
        f"0{n}.flac": SimpleNamespace(albumartist="The Artist", artist=["The Artist"], album="The Album", date="2020")
        for n in (1, 2)
    }
    sent: dict[str, Any] = {}

    async def search(searchstrs: list[str], **kwargs: Any) -> dict[str, Any]:
        sent.update(kwargs)
        return {}

    async def pick(*_args: Any) -> tuple[dict[str, Any], None]:
        return {}, None

    monkeypatch.setattr(metadata_mod, "run_metasearch", search)
    monkeypatch.setattr(metadata_mod, "pick_metadata", pick)
    rls_data = {"artists": [("The Artist", "main")], "title": "The Album"}
    anyio.run(metadata_mod.get_metadata, "/nowhere", tags, rls_data)
    assert sent["hints"] == search_hints(tags) == SearchHints("The Artist", "The Album", 2, "2020")
