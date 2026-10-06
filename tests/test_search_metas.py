"""`salmon metas` prints one line per release found. No store is contacted."""

import anyio

import salmon.search
from salmon.search import metas
from salmon.search.base import IdentData

RELEASE_IDS = {
    "Bandcamp": ("artist.bandcamp.com", "album", "the-album"),
    "MusicBrainz": "00000000-0000-0000-0000-000000000001",
    "Apple Music": ("us", "en-US", "1234567890"),
    "Discogs": 123456,
    "Beatport": 654321,
    "Qobuz": "abc123",
    "Tidal": ("US", 987654),
    "Deezer": 192837,
}


def test_metas_prints_every_result_url(monkeypatch, capsys) -> None:
    async def fake_run_metasearch(searchstrs, limit, track_count):
        return {
            source: {
                rls_id: (
                    IdentData("Artist", "The Album", 2024, 10, source),
                    f"[{source} edition]",
                )
            }
            for source, rls_id in RELEASE_IDS.items()
        }

    monkeypatch.setattr(salmon.search, "run_metasearch", fake_run_metasearch)
    assert metas.callback is not None

    anyio.run(metas.callback, ("artist", "album"), None, 3)

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("> ")]
    assert len(lines) == len(RELEASE_IDS)
    for source in RELEASE_IDS:
        assert f"> [{source} edition] " in out
    assert "https://artist.bandcamp.com/album/the-album" in out
    assert "/release/the-album/654321" in out
