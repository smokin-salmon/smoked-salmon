"""What track and disc number text reads as in common/numbers.py, and what changed on the way there."""

import os
import random
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from helpers.tagger_metadata_corrections import _scraped

import salmon.commands
from salmon import cfg
from salmon.commands import build_description
from salmon.common.numbers import (
    file_order_key,
    natural_key,
    number_text,
    padded_number,
    read_number,
    tag_number,
    tag_number_or_one,
    tag_text,
    unreadable_number,
)
from salmon.tagger.pre_data import create_track_list
from salmon.tagger.retagger import _names_distinct, _numbered_by_tags, _order_within_disc, generate_file_name


def _untagged(title: str, tracknumber: str | None = None, discnumber: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        artist=["Some Artist"],
        title=title,
        tracknumber=tracknumber,
        discnumber=discnumber,
        tracktotal=None,
        disctotal=None,
        replay_gain=None,
        peak=None,
        isrc=None,
    )


@pytest.mark.parametrize(
    ("value", "number"),
    [
        (None, None),
        ("", None),
        ("0", 0),
        ("-1", None),
        ("2/9", 2),
        (" 2", 2),
        ("02", 2),
        ("1²", None),
        ("１２", 12),
        ("A1", None),
        ("CD1", None),
        (2, 2),
        (["7"], 7),
        ([], None),
        ((2, 9), None),
    ],
)
def test_a_tag_value_reads_as_a_number_as_before(value: Any, number: int | None) -> None:
    assert read_number(value) == number


@pytest.mark.parametrize(
    ("value", "text"), [("2/9", "2"), (" 2 ", "2"), ("02", "02"), ("CD1/2", "CD1"), ("1/2/3", "1"), ("", "")]
)
def test_a_values_number_part_is_as_written_as_before(value: str, text: str) -> None:
    assert number_text(value) == text


@pytest.mark.parametrize(
    ("value", "number", "or_one", "text", "unreadable"),
    [
        (None, None, 1, "", None),
        ("", None, 1, "", None),
        ("  ", None, 1, "  ", None),
        ("0", 0, 0, "0", None),
        ("CD1", None, 1, "CD1", "CD1"),
        ("-1", None, 1, "-1", "-1"),
        ("1²", None, 1, "1²", "1²"),
        (["7", "8"], 7, 7, "7", None),
        ([], None, 1, "", None),
    ],
)
def test_each_reader_of_a_tag_answers_as_before(
    value: Any, number: int | None, or_one: int, text: str, unreadable: str | None
) -> None:
    for tags in ({"discnumber": value}, SimpleNamespace(discnumber=value)):
        assert tag_number(tags, "discnumber") == number
        assert tag_number_or_one(tags, "discnumber") == or_one
        assert tag_text(tags, "discnumber") == text
        assert unreadable_number(tags, "discnumber") == unreadable


def test_a_tag_object_with_no_such_field_has_no_number_as_before() -> None:
    assert tag_number(SimpleNamespace(), "discnumber") is None


@pytest.mark.parametrize(
    ("values", "ordered"),
    [
        (["CD10", "cd2", "CD1"], ["CD1", "cd2", "CD10"]),
        (["A10", "B1", "A2"], ["A2", "A10", "B1"]),
        (["1a", "1", ""], ["", "1", "1a"]),
        (["a", "1"], ["1", "a"]),
    ],
)
def test_digit_runs_sort_as_numbers_as_before(values: list[str], ordered: list[str]) -> None:
    assert sorted(values, key=natural_key) == ordered


def test_a_natural_key_reads_any_value_as_its_text_as_before() -> None:
    # As _disc_track_sort_key and _natural_track_key did. The retagger's _natural_key took only text.
    assert natural_key(10) == natural_key("10")
    assert natural_key("01") == natural_key("1")


def test_a_natural_key_orders_any_scripts_digits_by_value_as_before() -> None:
    assert sorted(["CD١٠", "CD٣"], key=natural_key) == ["CD٣", "CD١٠"]


@pytest.mark.parametrize(
    "names",
    [
        ["1.flac", "2.flac", "10.flac"],
        ["1-1.flac", "1-2.flac", "1-10.flac"],
        ["CD1/01.flac", "CD2/01.flac", "CD10/01.flac"],
        ["Disc 1/2.flac", "Disc 1/10.flac", "Disc 2/1.flac"],
        ["A1.flac", "A2.flac", "B1.flac"],
        ["CD1/01.flac", "CD1 Bonus/01.flac", "CD2/01.flac"],
        ["x 2.flac", "x 10.flac"],
        ["1/01.flac", "1 a.flac", "01.flac"],
    ],
)
def test_file_order_key_each_folder_then_the_name(names: list[str]) -> None:
    assert sorted(names[::-1], key=file_order_key) == names


@pytest.mark.parametrize(
    "names",
    [
        ["01.flac", "02.flac", "(bonus).flac"],
        ["01.flac", "02.flac", "!x.flac"],
        ["01.flac", "02.flac", "-1.flac"],
        ["01.flac", "02.flac", ".a.flac"],
        ["CD01/01.flac", "CD01-Bonus/01.flac", "CD02/01.flac"],
    ],
)
def test_file_order_key_moves_padded_names_that_text_puts_first(names: list[str]) -> None:
    assert sorted(names[::-1], key=file_order_key) == names
    assert sorted(names) != names


@pytest.mark.parametrize(
    "names",
    [["01.flac", "1.flac"], ["Track.flac", "track.flac"], ["CD01/a.flac", "CD1/a.flac"]],
)
def test_file_order_key_breaks_a_tie_by_the_exact_path(names: list[str]) -> None:
    assert sorted(names, key=file_order_key) == sorted(names[::-1], key=file_order_key) == names
    assert len({file_order_key(name) for name in names}) == len(names)
    # Without the path they tie: the path is what decides.
    assert len({file_order_key(name)[0] for name in names}) == 1


@pytest.mark.parametrize(
    "names",
    [
        ["٢.flac", "١٠.flac"],
        ["٢.flac", "10.flac"],
        ["２.flac", "１０.flac"],
        ["1.flac", "1².flac", "2.flac"],
    ],
)
def test_file_order_key_any_scripts_digits(names: list[str]) -> None:
    assert sorted(names[::-1], key=file_order_key) == names


def test_file_order_key_with_the_other_separator(monkeypatch) -> None:
    monkeypatch.setattr(os, "sep", "\\")
    names = ["CD1\\2.flac", "CD1\\10.flac", "CD1 Bonus\\01.flac", "CD2\\01.flac", "CD10\\01.flac"]
    assert sorted(names[::-1], key=file_order_key) == names
    # A walk from a top written with a forward slash: that slash is in every path, in one part they all share.
    prefixed = [f"C:/music/album\\{name}" for name in names]
    assert sorted(prefixed[::-1], key=file_order_key) == prefixed


def test_file_order_key_absolute_as_relative() -> None:
    names = ["CD1/01.flac", "CD1/02.flac", "CD2/01.flac", "CD10/01.flac"]
    absolute = [f"/music/Album/{name}" for name in names]
    assert sorted(absolute[::-1], key=file_order_key) == absolute


def test_file_order_key_one_order_from_any_shuffle() -> None:
    rng = random.Random(940)
    pieces = [
        "1",
        "2",
        "0",
        "10",
        "٣",
        "²",
        "é",
        "É",
        " ",
        ".",
        "-",
        "/",
        "a",
        "B",
        "\\",
        "_",
        "１",
        "ß",
        "İ",
        "\udcff",
    ]
    names = list({"".join(rng.choice(pieces) for _ in range(rng.randint(1, 12))) for _ in range(5000)})
    # No comparison of a number with text raises, and no two paths tie.
    ordered = sorted(names, key=file_order_key)
    assert len(ordered) > 4000
    for _ in range(5):
        assert sorted(rng.sample(names, len(names)), key=file_order_key) == ordered
    assert len({file_order_key(name) for name in names}) == len(names)


def test_a_tracks_place_goes_by_its_files_number_as_before() -> None:
    tags = {name: _untagged(title) for name, title in (("10 a.flac", "Ten"), ("2 b.flac", "Two"))}

    assert create_track_list(tags, False)["1"]["1"]["title"] == "Two"


def test_a_tracks_key_is_its_tags_number_part_as_before() -> None:
    tags = {"a.flac": _untagged("A", tracknumber="3/10")}

    assert create_track_list(tags, False)["1"]["3"]["numbered"] is True


def test_a_track_tagged_0_takes_its_place_as_before() -> None:
    tags = {"a.flac": _untagged("A", tracknumber="0")}

    assert create_track_list(tags, False)["1"]["1"]["numbered"] is False


@pytest.mark.parametrize(
    ("numbers", "numbered"),
    [(("1", "2"), True), (("2", "A1"), False), (("2", None), False), (("1", "0"), False), (("1", "1"), False)],
)
def test_only_files_with_their_own_readable_numbers_are_numbered_as_before(numbers, numbered: bool) -> None:
    tags = {f"{i}.flac": _untagged("A", tracknumber=n) for i, n in enumerate(numbers)}

    assert _numbered_by_tags(tags) is numbered


def test_a_missing_disc_is_not_read_as_disc_1_when_numbering_as_before() -> None:
    tags = {"a.flac": _untagged("A", tracknumber="1"), "b.flac": _untagged("B", tracknumber="1", discnumber="1")}

    assert _numbered_by_tags(tags) is True


def test_distinct_track_tags_order_a_disc_by_number_as_before() -> None:
    group = [("x.flac", _untagged("Ten", tracknumber="10")), ("y.flac", _untagged("Two", tracknumber="2"))]

    assert [name for name, _ in _order_within_disc(group, ["1", "2"])] == ["y.flac", "x.flac"]


@pytest.mark.parametrize(("names", "distinct"), [(["01.flac", "1.flac"], False), (["CD1", "cd2"], True)])
def test_names_that_differ_only_in_padding_are_not_told_apart_as_before(names: list[str], distinct: bool) -> None:
    assert _names_distinct(names) is distinct


@pytest.mark.parametrize(
    ("value", "shown"),
    [("1", "01"), ("010", "10"), ("100", "100"), ("A1", "A1"), ("", ""), (5, "05"), (None, "None"), (-1, "-1")],
)
def test_a_number_in_a_tracklist_is_padded_as_before(value: Any, shown: str) -> None:
    assert padded_number(value) == shown


@pytest.mark.parametrize(
    ("tags", "name"),
    [
        ({"tracknumber": ["2"], "title": ["T"]}, "02. T.flac"),
        ({"tracknumber": [None], "title": ["T"]}, "None. T.flac"),
        ({"title": ["T"]}, ". T.flac"),
        (SimpleNamespace(tracknumber="2", title="T"), "02. T.flac"),
        (SimpleNamespace(tracknumber=2, title="T"), "02. T.flac"),
        (SimpleNamespace(tracknumber=None, title="T"), "None. T.flac"),
    ],
)
def test_a_file_names_number_is_padded_as_before(monkeypatch, tags: Any, name: str) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}. {title}")

    assert generate_file_name(tags, ".flac", multiple_artists=False) == name


def test_a_superscript_in_a_tracklist_stays_as_written(monkeypatch) -> None:
    scraped = _scraped([("Dree Low", "main")], [("Einár", "main")])
    scraped["tracks"]["1"]["1²"] = {**scraped["tracks"]["1"].pop("2"), "track#": "1²"}

    async def fake_run_metadata(url, return_source_name=False):
        return scraped, "MusicBrainz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)

    description = anyio.run(build_description, ["https://musicbrainz.org/release/x"])

    assert "[b]1².[/b]" in description


def test_a_multi_disc_tracklist_pads_both_numbers_as_before(monkeypatch) -> None:
    scraped = _scraped([("Dree Low", "main")], [("Einár", "main")])
    scraped["tracks"]["2"] = {"1": {**scraped["tracks"]["1"].pop("2"), "disc#": "2", "track#": "1"}}

    async def fake_run_metadata(url, return_source_name=False):
        return scraped, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)

    description = anyio.run(build_description, ["https://www.qobuz.com/us-en/album/album/abc123"])

    assert "[b]02-01.[/b]" in description
