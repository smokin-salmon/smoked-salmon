"""A .torrent that cannot be written fails that tracker's upload, and says which file."""

from typing import TYPE_CHECKING, cast

import pytest
from test_torrent_name_normalization import FakeGazelleApi, _make_album
from torf import Torrent, WriteError

from salmon.errors import UploadRefusedError
from salmon.uploader.upload import generate_torrent

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


def test_a_folder_that_cannot_be_written_to_names_the_torrent_file(tmp_path) -> None:
    album = _make_album(tmp_path)
    in_the_way = tmp_path / "a file, not a folder"
    in_the_way.write_bytes(b"")
    site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(in_the_way))))

    with pytest.raises(UploadRefusedError, match="Could not write .*Album - TEST.torrent") as raised:
        generate_torrent(site, str(album))

    assert isinstance(raised.value.__cause__, WriteError)


def test_an_os_error_on_writing_says_why(tmp_path, monkeypatch) -> None:
    def write(_torrent: Torrent, _path: str, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Torrent, "write", write)
    site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(tmp_path))))

    with pytest.raises(UploadRefusedError, match="Could not write .*No space left"):
        generate_torrent(site, str(_make_album(tmp_path)))
