from typing import Any, cast

import anyio

from salmon.uploader.preassumptions import print_group_info


class FakeSite:
    async def torrentgroup(self, group_id):
        return {
            "group": {
                "name": "Hold Fast",
                "year": 2012,
                "recordLabel": "Fiction",
                "catalogueNumber": "CAT1",
                "musicInfo": {"artists": [{"name": "The Crookes"}]},
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
