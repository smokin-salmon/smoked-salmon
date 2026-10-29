from typing import Any, cast

import anyio

from salmon import cfg
from salmon.uploader.preassumptions import print_group_info


class FakeSite:
    def __init__(self, artists=("The Crookes",)):
        self.artists = artists

    async def torrentgroup(self, group_id):
        return {
            "group": {
                "name": "Hold Fast",
                "year": 2012,
                "recordLabel": "Fiction",
                "catalogueNumber": "CAT1",
                "musicInfo": {"artists": [{"name": name} for name in self.artists]},
            },
            "torrents": [
                {"media": "CD", "format": "FLAC", "encoding": "Lossless", "remasterYear": 2012},
                {"media": "WEB", "format": "FLAC", "encoding": "Lossless"},
            ],
        }


def test_print_group_info_without_remastered_key(capsys):
    anyio.run(print_group_info, cast("Any", FakeSite()), 1, "CD")

    out = capsys.readouterr().out
    assert "> 2012 / Fiction / CAT1 / CD / FLAC / Lossless" in out
    assert "WEB" not in out


def test_print_group_info_uses_the_various_artist_threshold(monkeypatch, capsys):
    # The group's artists became "Various" at a hard-coded four, whatever various_artist_threshold said.
    monkeypatch.setattr(cfg.upload.formatting, "various_artist_threshold", 3)

    anyio.run(print_group_info, cast("Any", FakeSite(("A", "B", "C"))), 1, "CD")
    out = capsys.readouterr().out

    assert f"(Group 1) {cfg.upload.formatting.various_artist_word} - Hold Fast" in out
