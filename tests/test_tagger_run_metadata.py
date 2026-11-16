"""What a scrape shown on its own holds: run_metadata, which `salmon meta` and salmon web's metadata lookup go
through, decides its credits as a merge of one source does and leaves out what the scrapers keep only for the merge
(Discogs ANVs, a stating store's guesses). descgen's scrapes keep them, for its merge.

Discogs is a fake on 127.0.0.1: the scraper's API address is pointed at it.
"""

import json
import stat
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import anyio
import pytest
from asyncclick.testing import CliRunner, Result
from helpers.tagger_run_metadata import GUESSED, RELEASE_URL, STATED, FakeDiscogs, pointed_at, stating_credits

import salmon.tagger
from salmon.errors import ScrapeError
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


def test_a_scrape_shown_on_its_own_holds_no_assumed_artists(monkeypatch: pytest.MonkeyPatch) -> None:
    stating_credits(monkeypatch)

    metadata, _ = in_fake_discogs(monkeypatch, lambda: run_metadata(RELEASE_URL))

    track = metadata["tracks"]["3"]["3"]
    assert "assumed_artists" not in track
    assert "artist_variations" not in track
    json.dumps(metadata)


def test_the_source_name_comes_with_the_same_scrape_without_assumed_artists(monkeypatch: pytest.MonkeyPatch) -> None:
    stating_credits(monkeypatch)

    (metadata, source), _ = in_fake_discogs(monkeypatch, lambda: run_metadata(RELEASE_URL, return_source_name=True))

    assert source == "Discogs"
    assert "assumed_artists" not in metadata["tracks"]["3"]["3"]


def test_a_scrape_shown_on_its_own_is_decided_as_a_merge_of_one_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """One credit per name: the store's stated main, not the guest it guessed, on the track and the release."""
    stating_credits(monkeypatch)

    metadata, _ = in_fake_discogs(monkeypatch, lambda: run_metadata(RELEASE_URL))

    assert metadata["tracks"]["3"]["3"]["artists"] == [("K-Ci & JoJo", "main"), ("Club Asylum", "remixer")]
    assert metadata["artists"] == [("K-Ci & JoJo", "main"), ("Club Asylum", "remixer")]


def test_a_scrape_for_the_merge_keeps_what_the_merge_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undecided too: the merge decides, over every source."""
    stating_credits(monkeypatch)

    (metadata, source), _ = in_fake_discogs(
        monkeypatch, lambda: run_metadata(RELEASE_URL, return_source_name=True, for_merge=True)
    )

    track = metadata["tracks"]["3"]["3"]
    assert source == "Discogs"
    assert track["artists"] == STATED
    assert track["assumed_artists"] == GUESSED
    assert track["artist_variations"] == [{"name": "K-Ci & JoJo", "role": "main", "anv": "K-Ci And JoJo"}]
    json.dumps(metadata)


def test_as_before_a_scraper_still_keeps_the_assumed_artists_for_the_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    stating_credits(monkeypatch)

    async def scrape() -> dict[str, Any]:
        return await discogs.Scraper().scrape_release(RELEASE_URL)

    metadata, _ = in_fake_discogs(monkeypatch, scrape)

    assert metadata["tracks"]["3"]["3"]["assumed_artists"] == GUESSED
    json.dumps(metadata)


def test_a_store_that_states_its_credits_hands_them_on_as_it_read_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """No per-track rewriting in the scrape: the merge decides. The release's credits are generate_artists', as any
    store's (here the same three: it lists each role a name has on the release)."""
    stating_credits(monkeypatch)

    async def scrape() -> dict[str, Any]:
        return await discogs.Scraper().scrape_release(RELEASE_URL)

    metadata, _ = in_fake_discogs(monkeypatch, scrape)

    assert metadata["tracks"]["3"]["3"]["artists"] == STATED
    assert metadata["artists"] == [("K-Ci & JoJo", "guest"), ("K-Ci & JoJo", "main"), ("Club Asylum", "remixer")]


def test_a_stating_scrape_types_its_release_from_its_artists_spelled_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compilation by two artists is an Anthology: "Bjork" and "Björk" are one of them, not a third."""
    stating_credits(monkeypatch)
    spelled_twice = [("Bjork", "main"), ("Björk", "main"), ("Carol", "main")]
    parse_tracks = discogs.Scraper.parse_tracks

    async def parsed(self: discogs.Scraper, soup: dict) -> dict:
        tracks = await parse_tracks(self, soup)
        tracks["3"]["3"]["artists"] = list(spelled_twice)
        return tracks

    monkeypatch.setattr(discogs.Scraper, "parse_tracks", parsed)

    async def scrape() -> dict[str, Any]:
        return await discogs.Scraper().scrape_release(RELEASE_URL)

    metadata, _ = in_fake_discogs(monkeypatch, scrape)

    assert metadata["tracks"]["3"]["3"]["artists"] == spelled_twice
    assert metadata["rls_type"] == "Anthology"


def test_as_before_a_store_that_states_nothing_has_one_credit_per_name_in_its_scrape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stating_credits(monkeypatch, states=False)

    async def scrape() -> dict[str, Any]:
        return await discogs.Scraper().scrape_release(RELEASE_URL)

    metadata, _ = in_fake_discogs(monkeypatch, scrape)

    assert metadata["tracks"]["3"]["3"]["artists"] == [("K-Ci & JoJo", "guest"), ("Club Asylum", "remixer")]


def test_salmon_meta_prints_no_assumed_artists(monkeypatch: pytest.MonkeyPatch) -> None:
    stating_credits(monkeypatch)

    async def run() -> Result:
        return await CliRunner().invoke(salmon.tagger.meta, [RELEASE_URL])

    result, _ = in_fake_discogs(monkeypatch, run)

    assert result.exit_code == 0, result.output
    assert "K-Ci & JoJo" in result.output
    assert "assumed_artists" not in result.output


def test_salmon_meta_saves_json_with_no_assumed_artists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stating_credits(monkeypatch)
    output_path = tmp_path / "metadata.json"

    async def run() -> Result:
        return await CliRunner().invoke(salmon.tagger.meta, [RELEASE_URL, "--output", str(output_path)])

    result, _ = in_fake_discogs(monkeypatch, run)

    assert result.exit_code == 0, result.output
    track = json.loads(output_path.read_text(encoding="utf-8"))["tracks"]["3"]["3"]
    assert track["artists"] == [["K-Ci & JoJo", "main"], ["Club Asylum", "remixer"]]
    assert "assumed_artists" not in track


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

    temporary_paths = list(tmp_path.glob(".salmon-*"))
    assert temporary_paths == []


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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows ignores the mode")
def test_salmon_meta_preserves_existing_output_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "metadata.json"
    output_path.write_text("Original contents", encoding="utf-8")
    output_path.chmod(0o740)

    async def run() -> Result:
        return await CliRunner().invoke(
            salmon.tagger.meta,
            [RELEASE_URL, "--output", str(output_path)],
        )

    result, _ = in_fake_discogs(monkeypatch, run)

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o740
