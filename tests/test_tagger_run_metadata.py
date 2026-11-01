"""What a scrape shown on its own holds: run_metadata, which `salmon meta`, descgen and salmon web's metadata
lookup go through, leaves out what the scrapers keep only for the merge with file tags (Discogs ANVs).

Discogs is a fake on 127.0.0.1: the scraper's API address is pointed at it.
"""

import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import anyio
import pytest
from aiohttp import web
from asyncclick.testing import CliRunner, Result
from test_ssrf import LOOPBACK, FakeStore  # pyright: ignore[reportMissingImports]

import salmon.tagger
from salmon import ssrf
from salmon.errors import ScrapeError
from salmon.tagger.sources import discogs, run_metadata

RELEASE_URL = "https://www.discogs.com/release/2520452-Various-Dance-Anthems"

# Discogs release 2520452, trimmed to the credits of track 3-3, which names its artist differently from Discogs.
RELEASE = {
    "title": "Dance Anthems",
    "artists": [{"name": "Various", "anv": "", "join": ""}],
    "genres": ["Electronic"],
    "year": 1999,
    "released": "1999-05-03",
    "formats": [{"name": "CD", "descriptions": ["Compilation"]}],
    "labels": [{"name": "Some Label", "catno": "SL 1"}],
    "tracklist": [
        {
            "position": "3-3",
            "type_": "track",
            "title": "Tell Me It's Real",
            "artists": [{"name": "K-Ci & JoJo", "anv": "K-Ci And JoJo", "join": ""}],
            "extraartists": [{"name": "Club Asylum", "anv": "", "role": "Remix"}],
        }
    ],
}


class FakeDiscogs(FakeStore):
    """The Discogs API: release 2520452, whatever is asked."""

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request.path)
        return web.json_response(RELEASE)


def pointed_at(monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)
    monkeypatch.setattr(discogs.Scraper, "url", store.url(""))


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


def test_salmon_meta_saves_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    output_path = tmp_path / "metadata.json"

    async def run() -> Result:
        return await CliRunner().invoke(
            salmon.tagger.meta,
            [RELEASE_URL, "--output", str(output_path)],
        )

    result, _ = in_fake_discogs(monkeypatch, run)

    assert result.exit_code == 0, result.output
    assert output_path.exists()

    metadata = json.loads(output_path.read_text(encoding="utf-8"))

    assert metadata["title"] == "Dance Anthems"
    track = metadata["tracks"]["3"]["3"]
    assert track["artists"] == [
        ["K-Ci & JoJo", "main"],
        ["Club Asylum", "remixer"],
    ]
    assert "artist_variations" not in track


def test_salmon_meta_preserves_existing_output_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "metadata.json"
    original_contents = "This is the original file."
    output_path.write_text(original_contents, encoding="utf-8")

    def fail_dump(metadata, file, **kwargs):
        file.write('{"partial":')
        raise TypeError("Simulated JSON serialization failure")

    monkeypatch.setattr(salmon.tagger.json, "dump", fail_dump)

    async def run() -> Result:
        return await CliRunner().invoke(
            salmon.tagger.meta,
            [RELEASE_URL, "--output", str(output_path)],
        )

    result, _ = in_fake_discogs(monkeypatch, run)

    assert result.exit_code != 0
    assert isinstance(result.exception, TypeError)
    assert output_path.read_text(encoding="utf-8") == original_contents

    temporary_files = list(tmp_path.glob(".salmon-*.tmp"))
    assert temporary_files == []


def test_salmon_meta_exits_with_error_when_scrape_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "metadata.json"

    async def fail_scrape(url: str) -> dict[str, Any]:
        raise ScrapeError("Test scrape failure")

    monkeypatch.setattr(salmon.tagger, "run_metadata", fail_scrape)

    async def run() -> Result:
        return await CliRunner().invoke(
            salmon.tagger.meta,
            [RELEASE_URL, "--output", str(output_path)],
        )

    result = anyio.run(run)

    assert result.exit_code == 1
    assert "Scrape failed: Test scrape failure" in result.output
    assert not output_path.exists()
