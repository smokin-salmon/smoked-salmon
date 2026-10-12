import asyncio

from salmon.uploader import _should_upload_16bit_only
from salmon.uploader.dupe_checker import _edition_matches, group_has_torrent

OTHER_EDITION_24 = {
    "media": "Vinyl",
    "format": "FLAC",
    "encoding": "24bit Lossless",
    "remasterYear": 1973,
    "remasterTitle": "",
    "remasterRecordLabel": "Odeon",
    "remasterCatalogueNumber": "1C 062-04 207",
}

PARLOPHONE_16 = {
    "media": "Vinyl",
    "format": "FLAC",
    "encoding": "Lossless",
    "remasterYear": 1965,
    "remasterTitle": "",
    "remasterRecordLabel": "Parlophone",
    "remasterCatalogueNumber": "OMHS 3001",
}

OUR_EDITION_24 = {
    "media": "Vinyl",
    "format": "FLAC",
    "encoding": "24bit Lossless",
    "remasterYear": 1966,
    "remasterTitle": "Reissue Germany SMO",
    "remasterRecordLabel": "Odeon",
    "remasterCatalogueNumber": "SMO 73 991",
}

OUR_EDITION_16 = {
    "media": "Vinyl",
    "format": "FLAC",
    "encoding": "Lossless",
    "remasterYear": 1966,
    "remasterTitle": "Reissue Germany SMO",
    "remasterRecordLabel": "Odeon",
    "remasterCatalogueNumber": "SMO 73 991",
}

OUR_EDITION = {
    "year": "1966",
    "edition_title": "Reissue Germany SMO",
    "label": "Odeon",
    "catno": "SMO 73 991",
}

METADATA = {
    "format": "FLAC",
    "year": "1966",
    "edition_title": "Reissue Germany SMO",
    "label": "Odeon",
    "catno": "SMO 73 991",
}

RLS_24 = {"encoding": "24bit Lossless"}


class FakeApi:
    site_string = "OPS"

    def __init__(self, torrents):
        self.torrents = torrents
        self.calls = []

    async def torrentgroup(self, group_id):
        self.calls.append(group_id)
        return {"torrents": self.torrents}


def test_different_edition_24bit_does_not_match():
    result = asyncio.run(
        group_has_torrent(FakeApi([OTHER_EDITION_24, PARLOPHONE_16]), 1, "Vinyl", "FLAC", "24bit Lossless", OUR_EDITION)
    )
    assert result is None


def test_same_edition_24bit_matches():
    result = asyncio.run(
        group_has_torrent(FakeApi([OUR_EDITION_24]), 1, "Vinyl", "FLAC", "24bit Lossless", OUR_EDITION)
    )
    assert result == OUR_EDITION_24


def test_each_edition_field_mismatch_disqualifies():
    variants = [
        {**OUR_EDITION_24, "remasterYear": 1965},
        {**OUR_EDITION_24, "remasterRecordLabel": "Parlophone"},
        {**OUR_EDITION_24, "remasterCatalogueNumber": "OMHS 3001"},
        {**OUR_EDITION_24, "remasterTitle": "Something Else"},
    ]
    for variant in variants:
        assert _edition_matches(variant, OUR_EDITION) is False
        result = asyncio.run(group_has_torrent(FakeApi([variant]), 1, "Vinyl", "FLAC", "24bit Lossless", OUR_EDITION))
        assert result is None


def test_empty_edition_fields_are_wildcards():
    torrent = {
        "media": "Vinyl",
        "format": "FLAC",
        "encoding": "24bit Lossless",
        "remasterYear": "",
        "remasterTitle": "",
        "remasterRecordLabel": "",
        "remasterCatalogueNumber": "",
    }
    result = asyncio.run(group_has_torrent(FakeApi([torrent]), 1, "Vinyl", "FLAC", "24bit Lossless", OUR_EDITION))
    assert result == torrent


def test_should_upload_16bit_only_when_same_edition_24_present():
    api = FakeApi([OUR_EDITION_24])
    result = asyncio.run(_should_upload_16bit_only(api, 1, "Vinyl", METADATA, RLS_24, True))
    assert result is True


def test_should_not_upload_16bit_only_when_16_already_present():
    api = FakeApi([OUR_EDITION_24, OUR_EDITION_16])
    result = asyncio.run(_should_upload_16bit_only(api, 1, "Vinyl", METADATA, RLS_24, True))
    assert result is False


def test_beatles_scenario_other_edition_only_returns_false():
    api = FakeApi([OTHER_EDITION_24, PARLOPHONE_16])
    result = asyncio.run(_should_upload_16bit_only(api, 1, "Vinyl", METADATA, RLS_24, True))
    assert result is False


def test_not_24bit_source_makes_no_api_call():
    api = FakeApi([OUR_EDITION_24])
    result = asyncio.run(_should_upload_16bit_only(api, 1, "Vinyl", METADATA, {"encoding": "Lossless"}, True))
    assert result is False
    assert api.calls == []
