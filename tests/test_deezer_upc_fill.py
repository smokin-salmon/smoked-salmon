"""A missing UPC is taken from the Deezer album the files came from, and from nowhere else (#545)."""

import anyio
import pytest

from salmon.errors import ScrapeError
from salmon.sources import deezer
from salmon.tagger import metadata as metadata_mod

DEEZER_URL = "https://www.deezer.com/en/album/322064097"
QOBUZ_URL = "https://www.qobuz.com/au-en/album/journaling-illy/ul39e7xjbuqrb"


def _deezer_answers(monkeypatch, payload):
    asked: list[str] = []

    async def fake_get_json(_self, url, params=None, headers=None):
        asked.append(url)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(deezer.DeezerBase, "get_json", fake_get_json)
    return asked


def test_album_upc_reads_the_album_by_its_id(monkeypatch) -> None:
    asked = _deezer_answers(monkeypatch, {"id": 322064097, "upc": "0656465465801"})

    upc = anyio.run(deezer.album_upc, DEEZER_URL)

    assert upc == "0656465465801"
    assert asked == ["/album/322064097"]


@pytest.mark.parametrize(
    "url",
    [
        QOBUZ_URL,
        "https://www.deezer.com/track/12345",
        "https://www.deezer.com/playlist/12345",
        "not a url",
        "https://notdeezer.com/album/322064097",
        "https://deezer.com.evil.test/album/322064097",
        "https://evil.com/?x=deezer.com/album/1",
        "https://www.deezer.com/album/322064097junk",
    ],
)
def test_album_upc_makes_no_request_for_anything_but_a_deezer_album(monkeypatch, url: str) -> None:
    asked = _deezer_answers(monkeypatch, {"upc": "should not be read"})

    upc = anyio.run(deezer.album_upc, url)

    assert upc is None
    assert asked == []


@pytest.mark.parametrize("failure", [ScrapeError("down"), TimeoutError()])
def test_a_failed_request_leaves_the_barcode_blank_and_says_so(monkeypatch, failure: Exception) -> None:
    # The barcode is optional, so an outage must not abort the upload; it must not pass for "no barcode" either.
    _deezer_answers(monkeypatch, failure)
    said: list[str] = []
    monkeypatch.setattr(deezer.click, "secho", lambda message, **_kwargs: said.append(message))

    upc = anyio.run(deezer.album_upc, DEEZER_URL)

    assert upc is None
    assert said and "Could not read the barcode" in said[0]


def test_an_album_without_a_barcode_says_nothing(monkeypatch) -> None:
    _deezer_answers(monkeypatch, {"id": 322064097, "upc": ""})
    said: list[str] = []
    monkeypatch.setattr(deezer.click, "secho", lambda message, **_kwargs: said.append(message))

    upc = anyio.run(deezer.album_upc, DEEZER_URL)

    assert upc is None
    assert said == []


def test_the_regex_still_reads_every_real_deezer_form() -> None:
    forms = [
        "https://www.deezer.com/album/322064097",
        "https://deezer.com/album/322064097",
        "https://www.deezer.com/en/album/322064097",
        "http://www.deezer.com/fr/album/322064097",
        "https://www.deezer.com/album/322064097?utm_source=x",
        "https://www.deezer.com/album/322064097/",
        "https://www.deezer.com/album/322064097#top",
    ]

    matches = [deezer.DeezerBase.regex.search(url) for url in forms]

    assert all(matches)
    assert [match[2] for match in matches if match] == ["322064097"] * len(forms)


def test_the_regex_rejects_a_lookalike_host_and_an_embedded_url() -> None:
    """evil.com/?x=deezer.com/album/1 and deezer.com.evil.test must not match (#545)."""
    lookalikes = [
        "https://evil.com/?x=deezer.com/album/1",
        "https://deezer.com.evil.test/album/1",
        "https://notdeezer.com/album/1",
    ]

    assert [deezer.DeezerBase.regex.search(url) for url in lookalikes] == [None, None, None]


class _FakeInfo:
    bits_per_sample = 16
    sample_rate = 44100


class _FakeAudio:
    def __init__(self, tags):
        self.tags = tags
        self.info = _FakeInfo()


def _tagged(monkeypatch, tags):
    """Make every audio file `tagger.tag_urls` reads carry the same tags."""
    from salmon.tagger import tag_urls as tag_urls_mod

    monkeypatch.setattr(tag_urls_mod, "MutagenFile", lambda _path: _FakeAudio(tags))
    monkeypatch.setattr(tag_urls_mod, "get_audio_files", lambda _path, _sort=False: ["01.flac"])


def test_fill_upc_from_deezer_fills_only_a_missing_upc(tmp_path, monkeypatch) -> None:
    (tmp_path / "01.flac").write_bytes(b"")
    _tagged(monkeypatch, {"source": [DEEZER_URL]})
    asked = _deezer_answers(monkeypatch, {"upc": "0656465465801"})

    missing = {"upc": None}
    present = {"upc": "1111111111111"}

    anyio.run(metadata_mod.fill_upc_from_deezer, missing, str(tmp_path))
    anyio.run(metadata_mod.fill_upc_from_deezer, present, str(tmp_path))

    assert missing["upc"] == "0656465465801"
    assert present["upc"] == "1111111111111"
    assert asked == ["/album/322064097"]


def test_fill_upc_from_deezer_leaves_a_non_deezer_source_alone(tmp_path, monkeypatch) -> None:
    (tmp_path / "01.flac").write_bytes(b"")
    _tagged(monkeypatch, {"source": [QOBUZ_URL]})
    asked = _deezer_answers(monkeypatch, {"upc": "should not be read"})

    metadata = {"upc": None}
    anyio.run(metadata_mod.fill_upc_from_deezer, metadata, str(tmp_path))

    assert metadata["upc"] is None
    assert asked == []


def test_fill_upc_from_deezer_makes_no_request_with_no_store_url(tmp_path, monkeypatch) -> None:
    (tmp_path / "01.flac").write_bytes(b"")
    _tagged(monkeypatch, {"title": ["A Song"]})
    asked = _deezer_answers(monkeypatch, {"upc": "should not be read"})

    metadata = {"upc": None}
    anyio.run(metadata_mod.fill_upc_from_deezer, metadata, str(tmp_path))

    assert metadata["upc"] is None
    assert asked == []
