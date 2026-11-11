"""DSD files in the tagger (#919): their ID3 tags are read and written through the MP3 fields, in the place each
container keeps them; their format and encoding come from the files; the folder check keeps them."""

import anyio
import asyncclick as click
import av
import pytest
from helpers.dsd import DSD64, DSD128, write_dff, write_dsf
from mutagen import File as MutagenFile
from test_folderstructure_yes_all import _check_under_yyy, _names

from salmon.tagger.foldername import _fix_format, resolution_token
from salmon.tagger.pre_data import parse_encoding, parse_format
from salmon.tagger.tagfile import TagFile
from salmon.tagger.tags import print_a_tag

TAGS = {"TIT2": "One", "TPE1": "Artist", "TALB": "Album", "TRCK": "1/2", "TDRC": "1963"}
# Where each container's header is: a tag written over or before it breaks the file.
MAGIC = {".dsf": b"DSD ", ".dff": b"FRM8"}
WRITERS = {".dsf": write_dsf, ".dff": write_dff}


def _samples(path) -> int:
    with av.open(str(path)) as container:
        return sum(frame.samples for frame in container.decode(audio=0))


@pytest.mark.parametrize("ext", [".dsf", ".dff"])
def test_reads_a_dsd_files_tags(tmp_path, ext: str) -> None:
    tags = TagFile(str(WRITERS[ext](tmp_path / f"01{ext}", tags=TAGS)))

    assert (tags.title, tags.artist, tags.album) == ("One", ["Artist"], "Album")
    assert (tags.tracknumber, tags.tracktotal, tags.date) == ("1", "2", "1963")
    assert tags.has_field("title")


@pytest.mark.parametrize("ext", [".dsf", ".dff"])
@pytest.mark.parametrize("tags", [None, {}], ids=["no tag", "an empty tag"])
def test_writes_tags_where_the_container_keeps_them(tmp_path, ext: str, tags: dict | None) -> None:
    path = WRITERS[ext](tmp_path / f"01{ext}", tags=tags)
    samples = _samples(path)

    tagfile = TagFile(str(path))
    tagfile.title = "New"
    tagfile.artist = ["A", "B"]
    tagfile.tracknumber = "3"
    assert tagfile.save()

    # The header is still first: a plain ID3 tag saved through the DSF would have gone before it.
    assert path.read_bytes()[:4] == MAGIC[ext]
    assert _samples(path) == samples
    again = TagFile(str(path))
    assert (again.title, again.artist, again.tracknumber) == ("New", ["A", "B"], "3")
    saved = MutagenFile(str(path))
    assert saved is not None
    assert type(saved.tags).__name__ in ("_DSFID3", "_DSDIFFID3")


def test_rewriting_a_tag_keeps_the_others(tmp_path) -> None:
    path = write_dsf(tmp_path / "01.dsf", tags=TAGS)

    tagfile = TagFile(str(path))
    tagfile.title = "Renamed"
    tagfile.tracktotal = "5"
    tagfile.save()

    again = TagFile(str(path))
    assert (again.title, again.album, again.tracknumber, again.tracktotal) == ("Renamed", "Album", "1", "5")


def test_prints_a_dsd_files_tags(tmp_path, capsys) -> None:
    print_a_tag(TagFile(str(write_dsf(tmp_path / "01.dsf", tags=TAGS))))

    printed = capsys.readouterr().out
    assert "> title: One" in printed
    assert "> artist: Artist" in printed


def test_a_dsd_release_is_format_dsd_with_its_rate_as_encoding() -> None:
    audio_info = {name: {"sample rate": DSD128, "precision": 1} for name in ("01.dsf", "02.dff")}

    assert parse_format("01 - One.DSF") == parse_format("01.dff") == "DSD"
    # Whatever -e said: the files say it.
    assert anyio.run(parse_encoding, "DSD", audio_info, ["320", False], True) == ("DSD128", False)


def test_a_dsd_release_of_two_rates_stops(capsys) -> None:
    audio_info = {
        "01.dsf": {"sample rate": DSD64, "precision": 1},
        "02.dsf": {"sample rate": DSD128, "precision": 1},
    }

    with pytest.raises(click.Abort):
        anyio.run(parse_encoding, "DSD", audio_info, None, True)
    assert "The DSD files are at 2.8224 MHz, 5.6448 MHz" in capsys.readouterr().out


def test_a_dsd_folder_is_named_by_its_rate() -> None:
    metadata = {"format": "DSD", "encoding": "DSD256", "encoding_vbr": False}
    assert _fix_format(metadata, ["format"])["format"] == "DSD256"
    # No "1-11289.6" resolution: the format names the rate.
    assert resolution_token({"01.dsf": {"precision": 1, "sample rate": 11289600}}) == ""


def test_the_folder_check_keeps_dsd_files_under_yyy(tmp_path) -> None:
    root = tmp_path / "Artist - Album [WEB DSD64]"
    root.mkdir()
    write_dsf(root / "01. One.dsf")
    write_dff(root / "02. Two.dff")
    (root / "cover.jpg").write_bytes(b"jpg")

    _check_under_yyy(root)

    assert _names(root) == ["01. One.dsf", "02. Two.dff", "cover.jpg"]


def test_the_folder_check_stops_on_dsd_beside_flac_under_yyy(tmp_path, capsys) -> None:
    root = tmp_path / "Artist - Album [WEB]"
    root.mkdir()
    write_dsf(root / "01. One.dsf")
    (root / "02. Two.flac").write_bytes(b"fLaC")

    with pytest.raises(click.Abort):
        _check_under_yyy(root)
    assert "multiple audio codecs" in capsys.readouterr().out
