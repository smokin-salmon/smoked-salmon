"""salmon transcode and downconv on a DSD album (#919): they convert FLAC only, and stop before making any output,
which would have been the .dsf files copied under an [MP3 V0] or [FLAC] name."""

import os

import anyio
import asyncclick as click
import pytest
from helpers.dsd import write_dsf

from salmon.converter.downconverting import convert_folder
from salmon.converter.transcoding import transcode_folder


@pytest.mark.parametrize(
    "convert",
    [lambda path: transcode_folder(path, "V0"), lambda path: convert_folder(path, 16)],
    ids=["transcode", "downconv"],
)
def test_a_dsd_album_is_not_converted(tmp_path, capsys, convert) -> None:
    album = tmp_path / "Artist - Album (2020) [WEB DSD64]"
    album.mkdir()
    write_dsf(album / "01.dsf")
    (album / "cover.jpg").write_bytes(b"jpg")

    with pytest.raises(click.Abort):
        anyio.run(convert, str(album))

    assert os.listdir(tmp_path) == [album.name]
    assert "A DSD file was found in the folder (01.dsf)" in capsys.readouterr().out
