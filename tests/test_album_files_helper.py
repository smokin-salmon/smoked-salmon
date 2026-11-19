"""tests/helpers/album_files.py writes the FLACs and albums new tests build on."""

from pathlib import Path

from helpers.album_files import album, write_flac
from mutagen.flac import FLAC


def test_write_flac_has_the_streaminfo_and_tags_it_was_given(tmp_path: Path) -> None:
    path = write_flac(tmp_path / "new" / "deep" / "a.flac", bits=24, rate=96000, TITLE="One", ARTIST="Artist")

    tagged = FLAC(path)
    assert (tagged.info.bits_per_sample, tagged.info.sample_rate) == (24, 96000)
    assert (tagged["TITLE"], tagged["ARTIST"]) == (["One"], ["Artist"])


def test_write_flac_defaults_to_cd_quality(tmp_path: Path) -> None:
    info = FLAC(write_flac(tmp_path / "a.flac")).info

    assert (info.bits_per_sample, info.sample_rate) == (16, 44100)


def test_album_names_numbers_and_tags_its_tracks(tmp_path: Path) -> None:
    folder = album(tmp_path / "Album", tracks=3, tags={"ALBUM": "Name"})

    names = ["01.flac", "02.flac", "03.flac"]
    assert sorted(p.name for p in folder.iterdir()) == names
    for number, name in enumerate(names, start=1):
        tagged = FLAC(folder / name)
        assert tagged["TRACKNUMBER"] == [str(number)]
        assert tagged["ALBUM"] == ["Name"]


def test_album_writes_a_cover_only_when_asked(tmp_path: Path) -> None:
    without = album(tmp_path / "without")
    with_cover = album(tmp_path / "with", cover=True)

    assert not (without / "cover.jpg").exists()
    assert (with_cover / "cover.jpg").read_bytes() == b"jpeg"
    assert len(list(with_cover.glob("*.flac"))) == 2


def test_an_empty_album_is_still_a_folder_and_can_hold_a_cover(tmp_path: Path) -> None:
    bare = album(tmp_path / "bare", tracks=0)
    covered = album(tmp_path / "covered", tracks=0, cover=True)

    assert list(bare.iterdir()) == []
    assert [p.name for p in covered.iterdir()] == ["cover.jpg"]
