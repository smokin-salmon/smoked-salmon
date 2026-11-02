"""What a scrape shown on its own holds: run_metadata, which `salmon meta`, descgen and salmon web's metadata
lookup go through, leaves out what the scrapers keep only for the merge with file tags (Discogs ANVs).

Discogs is a fake on 127.0.0.1: the scraper's API address is pointed at it.
"""

import json
from collections.abc import Callable, Coroutine
from typing import Any

import anyio
import pytest
from asyncclick.testing import CliRunner, Result
from helpers.tagger_run_metadata import RELEASE_URL, FakeDiscogs, pointed_at

import salmon.tagger
from salmon.tagger.sources import discogs, run_metadata


def in_fake_discogs(
    monkeypatch: pytest.MonkeyPatch, run: Callable[[], Coroutine[Any, Any, Any]]
) -> tuple[Any, FakeDiscogs]:
    store = FakeDiscogs()

    async def main() -> Any:
        async with store.serving():
            pointed_at(monkeypatch, store)
            return await run()

    return anyio.run(main), store


def test_a_scrape_shown_on_its_own_holds_no_artist_variations(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata, store = in_fake_discogs(monkeypatch, lambda: run_metadata(RELEASE_URL))

    assert store.requests == ["/releases/2520452"]
    track = metadata["tracks"]["3"]["3"]
    assert track["artists"] == [("K-Ci & JoJo", "main"), ("Club Asylum", "remixer")]
    assert "artist_variations" not in track
    # All of it is plain data: nothing in a scrape may stop it being shown as JSON.
    json.dumps(metadata)


def test_the_source_name_comes_with_the_same_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    (metadata, source), _ = in_fake_discogs(monkeypatch, lambda: run_metadata(RELEASE_URL, return_source_name=True))

    assert source == "Discogs"
    assert "artist_variations" not in metadata["tracks"]["3"]["3"]


def test_a_scraper_still_keeps_the_artist_variations_for_the_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scrape() -> dict[str, Any]:
        return await discogs.Scraper().scrape_release(RELEASE_URL)

    metadata, _ = in_fake_discogs(monkeypatch, scrape)

    assert metadata["tracks"]["3"]["3"]["artist_variations"] == [
        {"name": "K-Ci & JoJo", "role": "main", "anv": "K-Ci And JoJo"}
    ]
    # Plain data too, so a path that does show a scrape cannot fail on it.
    json.dumps(metadata)


def test_salmon_meta_prints_no_artist_variations(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> Result:
        return await CliRunner().invoke(salmon.tagger.meta, [RELEASE_URL])

    result, _ = in_fake_discogs(monkeypatch, run)

    assert result.exit_code == 0, result.output
    assert "K-Ci & JoJo" in result.output
    assert "artist_variations" not in result.output
    assert "K-Ci And JoJo" not in result.output
