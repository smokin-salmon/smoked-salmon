"""Which formats a tracker's edition already holds, for "upload only what's missing" (#1169).

edition_holdings reads the group listing the group question printed, and decides for the source and each conversion,
with a reason. A format counts as held only when a torrent of our edition (year, edition title), media, format and
encoding is in the group.
"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.uploader.dupe_checker import matching_torrents
from salmon.uploader.missing_formats import edition_holdings

RELEASE = {"source": "WEB", "format": "FLAC", "encoding": "24bit Lossless", "year": 2020, "edition_title": ""}
CONVERSIONS = {"16bit 44.1 kHz": ("FLAC", "Lossless"), "MP3 V0": ("MP3", "V0 (VBR)")}


def _torrent(tid: int, fmt: str, encoding: str, year: int = 2020, title: str = "", media: str = "WEB") -> dict:
    return {
        "id": tid,
        "media": media,
        "format": fmt,
        "encoding": encoding,
        "remastered": True,
        "remasterYear": year,
        "remasterTitle": title,
        "remasterRecordLabel": "Label",
        "remasterCatalogueNumber": "CAT1",
    }


def _group(*torrents: dict) -> dict:
    return {"group": {"id": 7, "year": 2020}, "torrents": list(torrents)}


def test_a_held_source_is_named_with_its_torrent() -> None:
    holdings = edition_holdings(_group(_torrent(1, "FLAC", "24bit Lossless")), RELEASE, CONVERSIONS)
    assert holdings.source.torrent is not None
    assert holdings.source.torrent["id"] == 1
    assert "2020 / Label / CAT1 / WEB / FLAC / 24bit Lossless" in holdings.source.reason
    assert [c.torrent for c in holdings.conversions] == [None, None]
    assert not holdings.nothing_missing
    assert holdings.held_conversions == set()


def test_a_held_conversion_is_held_and_the_source_missing() -> None:
    holdings = edition_holdings(_group(_torrent(2, "FLAC", "Lossless")), RELEASE, CONVERSIONS)
    assert holdings.source.torrent is None
    assert "no torrent of this edition is WEB / FLAC / 24bit Lossless" in holdings.source.reason
    assert holdings.held_conversions == {"16bit 44.1 kHz"}
    by_name = {c.name: c for c in holdings.conversions}
    assert "WEB / FLAC / Lossless" in by_name["16bit 44.1 kHz"].reason
    assert by_name["MP3 V0"].torrent is None


def test_nothing_held() -> None:
    holdings = edition_holdings(_group(), RELEASE, CONVERSIONS)
    assert holdings.source.torrent is None
    assert holdings.held_conversions == set()
    assert not holdings.nothing_missing


def test_everything_held_leaves_nothing_missing() -> None:
    group = _group(
        _torrent(1, "FLAC", "24bit Lossless"), _torrent(2, "FLAC", "Lossless"), _torrent(3, "MP3", "V0 (VBR)")
    )
    holdings = edition_holdings(group, RELEASE, CONVERSIONS)
    assert holdings.nothing_missing
    assert holdings.held_conversions == set(CONVERSIONS)


def test_a_held_source_and_no_conversion_wanted_leaves_nothing_missing() -> None:
    assert edition_holdings(_group(_torrent(1, "FLAC", "24bit Lossless")), RELEASE, {}).nothing_missing


def test_another_edition_holding_the_format_is_not_held() -> None:
    group = _group(
        _torrent(1, "FLAC", "24bit Lossless", year=2015),
        _torrent(2, "FLAC", "Lossless", title="Deluxe"),
        _torrent(3, "MP3", "V0 (VBR)", media="CD"),
    )
    holdings = edition_holdings(group, {**RELEASE, "edition_title": "Standard"}, CONVERSIONS)
    assert holdings.source.torrent is None
    assert holdings.held_conversions == set()


def test_a_search_row_reads_the_year_from_group_year() -> None:
    row = {
        "groupId": 7,
        "groupYear": 2015,
        "torrents": [
            {
                **_torrent(1, "FLAC", "24bit Lossless"),
                "remastered": False,
                "remasterYear": 0,
                "remasterTitle": "",
                "remasterRecordLabel": "",
                "remasterCatalogueNumber": "",
            }
        ],
    }
    assert edition_holdings(row, RELEASE, {}).source.torrent is None
    assert edition_holdings(row, {**RELEASE, "year": 2015}, {}).source.torrent is not None


_FORMATS = [("FLAC", "24bit Lossless"), ("FLAC", "Lossless"), ("MP3", "320"), ("MP3", "V0 (VBR)")]


def _random_group(rng: random.Random) -> dict:
    return _group(
        *(
            _torrent(
                rng.randrange(1, 10_000),
                *rng.choice(_FORMATS),
                year=rng.choice([2015, 2020]),
                title=rng.choice(["", "Deluxe"]),
                media=rng.choice(["WEB", "CD"]),
            )
            for _ in range(rng.randrange(9))
        )
    )


@pytest.mark.parametrize("seed", range(300))
def test_a_format_is_held_only_when_the_edition_holds_it(seed: int) -> None:
    rng = random.Random(seed)
    group = _random_group(rng)
    edition_title = rng.choice(["", "Deluxe"])
    release = {**RELEASE, "edition_title": edition_title}
    holdings = edition_holdings(group, release, CONVERSIONS)
    wanted = {"source": ("FLAC", "24bit Lossless"), **CONVERSIONS}
    for holding in (holdings.source, *holdings.conversions):
        fmt, encoding = wanted[holding.name if holding is not holdings.source else "source"]
        in_edition = matching_torrents(group, {**release, "format": fmt, "encoding": encoding})
        assert (holding.torrent is not None) == bool(in_edition)
        if holding.torrent is not None:
            assert holding.torrent in in_edition
    assert holdings.nothing_missing == all(h.torrent is not None for h in (holdings.source, *holdings.conversions))
