"""`salmon descgen` closes every BBCode tag it opens, and turns away what is not a store URL. No source is contacted."""

import re

import anyio
import pytest
from asyncclick.testing import CliRunner

import salmon.commands
from salmon import cfg
from salmon.commands import descgen
from salmon.tagger.sources import METASOURCES

METADATA = {
    "artists": [],
    "tracks": {
        "1": {
            "1": {"artists": [("Artist", "main")], "title": "First"},
            "2": {"artists": [("Artist", "main")], "title": "Second"},
        }
    },
    "comment": None,
    "urls": [],
}


def test_descgen_closes_the_tracklist_heading_size(monkeypatch, capsys) -> None:
    async def fake_run_metadata(url, return_source_name=False):
        return METADATA, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)
    monkeypatch.setattr(salmon.commands, "combine_metadatas", lambda *_: METADATA)
    monkeypatch.setattr(salmon.commands, "clean_metadata", lambda metadata: metadata)
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", False)
    assert descgen.callback is not None

    anyio.run(descgen.callback, ("https://www.qobuz.com/album/x",))

    out = capsys.readouterr().out
    assert "[b][size=4]Tracklist[/size][/b]" in out
    assert len(re.findall(r"\[size=\d+\]", out)) == len(re.findall(r"\[/size\]", out))


def _run_descgen(monkeypatch, *args: str):
    async def no_scrape(url, return_source_name=False):
        raise AssertionError(f"descgen scraped {url}")

    monkeypatch.setattr(salmon.commands, "run_metadata", no_scrape)

    async def run():
        return await CliRunner().invoke(descgen, list(args))

    return anyio.run(run)


def test_descgen_given_a_folder_says_it_takes_release_urls(monkeypatch, tmp_path) -> None:
    album = tmp_path / "Artist - Album (2024) [FLAC]"
    album.mkdir()

    result = _run_descgen(monkeypatch, str(album))

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert f"{album} is a folder; descgen takes release URLs from " in lines[0]
    assert "`salmon up` writes the description itself" in lines[0]
    assert all(name in lines[0] for name in METASOURCES)


def test_descgen_given_an_unsupported_url_lists_the_sources(monkeypatch) -> None:
    url = "https://shop.example.com/releases/123"

    result = _run_descgen(monkeypatch, url, "https://www.qobuz.com/us-en/album/x/abc123")

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "Argument 1 (shop.example.com) is not a release URL descgen supports" in lines[0]
    assert all(name in lines[0] for name in METASOURCES)


@pytest.mark.parametrize(
    "url",
    [
        "https://tracker.example/12345678901234567890abcdef/announce",
        "https://tracker.example/torrents.php?action=download&id=1&authkey=SECRETKEY&torrent_pass=SECRETPASS",
        "not a url, but SECRETKEY",
    ],
)
def test_descgen_never_echoes_a_refused_url(monkeypatch, url: str) -> None:
    result = _run_descgen(monkeypatch, "https://www.qobuz.com/us-en/album/x/abc123", url)

    assert result.exit_code == 1
    assert "Argument 2 (" in result.output
    assert "SECRET" not in result.output
    assert "announce" not in result.output
    assert "12345678901234567890abcdef" not in result.output
