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
from salmon.common.strings import comparable
from salmon.uploader import dupe_checker
from salmon.uploader.dupe_checker import describe_torrent, held_in_group, matching_torrents, remaster_text
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


def test_a_picked_source_counts_only_what_its_own_edition_holds() -> None:
    # Two FLACs of our year, filed under two catalogue numbers: only CAT1's edition has the transcodes.
    cat1_flac = _torrent(1, "FLAC", "24bit Lossless")
    cat2_flac = {**_torrent(4, "FLAC", "24bit Lossless"), "remasterCatalogueNumber": "CAT2"}
    group = _group(cat1_flac, cat2_flac, _torrent(2, "FLAC", "Lossless"), _torrent(3, "MP3", "V0 (VBR)"))

    # Unpicked, every match counts: the tracker would be skipped with nothing missing.
    assert edition_holdings(group, RELEASE, CONVERSIONS).nothing_missing
    picked = edition_holdings(group, RELEASE, CONVERSIONS, source=cat2_flac)
    assert picked.source.torrents == (cat2_flac,)
    assert picked.held_conversions == set()
    assert not picked.nothing_missing
    assert edition_holdings(group, RELEASE, CONVERSIONS, source=cat1_flac).nothing_missing


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


# One decider (#1192): held_formats, for --skip-flac-upload and cross-upload, asks edition_holdings. The oracles
# below are both deciders as they stood before, copied from dupe_checker and missing_formats at 020d250f.
def _old_site_edition(torrent: dict, group: dict) -> tuple[str, ...]:
    fields = remaster_text(torrent)
    if fields is None:
        info = group.get("group") or {}
        fields = (info.get("year"), "", info.get("recordLabel"), info.get("catalogueNumber"))
    return tuple(comparable(str(field or "")) for field in fields)


def _old_held_formats(group, release, source_flac, formats, *, same_edition=False) -> set[str]:
    source_id = str(source_flac.get("id"))
    placed = None
    if same_edition:
        placed = next((t for t in group.get("torrents") or [] if str(t.get("id")) == source_id), None)
    filed_under = _old_site_edition(placed, group) if placed is not None else None
    held = set()
    for name, (fmt, encoding) in formats.items():
        in_edition = matching_torrents(group, {**release, "format": fmt, "encoding": encoding})
        if any(
            str(t.get("id")) != source_id and (filed_under is None or _old_site_edition(t, group) == filed_under)
            for t in in_edition
        ):
            held.add(name)
    return held


def _old_edition_holdings(rset, release, conversions, source=None) -> list[tuple[str, tuple[dict, ...], str]]:
    group_info = rset.get("group") or {}
    filed_under = _old_site_edition(source, rset) if source is not None else None

    def decide(name: str, fmt: str, encoding: str) -> tuple[str, tuple[dict, ...], str]:
        held = tuple(
            torrent
            for torrent in held_in_group(rset, {**release, "format": fmt, "encoding": encoding})
            if filed_under is None or _old_site_edition(torrent, rset) == filed_under
        )
        if held:
            return name, held, f"this edition has {describe_torrent(held[0], group_info)}"
        return name, (), f"no torrent of this edition is {release.get('source')} / {fmt} / {encoding}"

    first = (
        ("source", (source,), f"this edition has {describe_torrent(source, group_info)}")
        if source is not None
        else decide("source", release.get("format") or "", release.get("encoding") or "")
    )
    return [first, *(decide(name, fmt, encoding) for name, (fmt, encoding) in conversions.items())]


def _filed_group(rng: random.Random) -> dict:
    """A torrentgroup response whose torrents spread over editions told apart by any of their fields."""
    torrents = []
    for tid in rng.sample(range(1, 10_000), rng.randrange(9)):
        fmt, encoding = rng.choice(_FORMATS)
        torrent = {"id": tid, "media": rng.choice(["WEB", "CD"]), "format": fmt, "encoding": encoding}
        if rng.random() < 0.7:
            torrent |= {
                "remastered": True,
                "remasterYear": rng.choice([2015, 2020, 0]),
                "remasterTitle": rng.choice(["", "Deluxe"]),
                "remasterRecordLabel": rng.choice(["Label", ""]),
                "remasterCatalogueNumber": rng.choice(["CAT1", "cat-1", "CAT2", ""]),
            }
        torrents.append(torrent)
    return {"group": {"id": 7, "year": 2020, "recordLabel": "Label", "catalogueNumber": "CAT1"}, "torrents": torrents}


def _source_flac(rng: random.Random, group: dict) -> dict:
    """The source as each caller passes it: a torrent of the group, its id alone or as text, or one not listed."""
    if group["torrents"] and rng.random() < 0.75:
        torrent = rng.choice(group["torrents"])
        return rng.choice([torrent, {"id": torrent["id"]}, {"id": str(torrent["id"])}])
    return {"id": 99_999}


@pytest.mark.parametrize("seed", range(500))
def test_held_formats_still_decide_as_before_through_the_one_decider(seed: int) -> None:
    rng = random.Random(seed)
    group = _filed_group(rng)
    release = {**RELEASE, "year": rng.choice([2020, "2015", ""]), "edition_title": rng.choice(["", "deluxe"])}
    # The source's own format may be among them: the source never holds it.
    formats = {f"{fmt} {encoding}": (fmt, encoding) for fmt, encoding in rng.sample(_FORMATS, rng.randrange(5))}
    source = _source_flac(rng, group)
    same_edition = rng.random() < 0.5

    expected = _old_held_formats(group, release, source, formats, same_edition=same_edition)

    assert dupe_checker.held_formats(group, release, source, formats, same_edition=same_edition) == expected
    holdings = edition_holdings(group, release, formats, source=source, same_edition=same_edition)
    assert holdings.held_conversions == expected


@pytest.mark.parametrize("seed", range(500))
def test_upload_only_missing_still_decides_as_before(seed: int) -> None:
    # Its callers' inputs: no source, or one of the group's FLACs of our edition, and conversions of other formats.
    rng = random.Random(seed)
    group = _filed_group(rng)
    release = {**RELEASE, "year": rng.choice([2020, "2015", ""]), "edition_title": rng.choice(["", "deluxe"])}
    source = rng.choice([None, *matching_torrents(group, release)])

    holdings = edition_holdings(group, release, CONVERSIONS, source=source)

    found = [(h.name, h.torrents, h.reason) for h in (holdings.source, *holdings.conversions)]
    assert found == _old_edition_holdings(group, release, CONVERSIONS, source)


def test_the_source_never_holds_a_conversion_of_its_own_format() -> None:
    flac = _torrent(1, "FLAC", "Lossless")
    holdings = edition_holdings(_group(flac), RELEASE, {"16bit": ("FLAC", "Lossless")}, source=flac)
    assert holdings.held_conversions == set()


def test_a_source_the_group_does_not_list_leaves_the_edition_to_year_and_title() -> None:
    # Our FLAC, not in the listing yet: its catalogue number does not narrow what counts.
    ours = {**_torrent(9, "FLAC", "24bit Lossless"), "remasterCatalogueNumber": "CAT2"}
    holdings = edition_holdings(_group(_torrent(3, "MP3", "V0 (VBR)")), RELEASE, CONVERSIONS, source=ours)
    assert holdings.held_conversions == {"MP3 V0"}
    assert "2020 / Label / CAT2 / WEB / FLAC / 24bit Lossless" in holdings.source.reason
    by_id = edition_holdings(_group(), RELEASE, CONVERSIONS, source={"id": 9})
    assert by_id.held_conversions == set()
    assert by_id.source.reason == "torrent 9 is the source"


def test_a_source_with_no_id_is_no_torrent_without_one() -> None:
    # A torrent named by another field (a search row's torrentId) is not the source for lacking an id.
    flac, mp3 = (
        {k: v for k, v in _torrent(tid, fmt, enc).items() if k != "id"}
        for tid, fmt, enc in ((1, "FLAC", "24bit Lossless"), (2, "MP3", "V0 (VBR)"))
    )
    holdings = edition_holdings(_group(flac, mp3), RELEASE, CONVERSIONS, source=flac)
    assert holdings.held_conversions == {"MP3 V0"}
