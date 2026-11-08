"""A track mutagen cannot read stops the run with an error that names it, not a traceback."""

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from helpers.tagger_tags import _write_flac

from salmon.cross_upload import _track_data
from salmon.errors import UploadError
from salmon.tagger import audio_info as audio_info_module
from salmon.tagger.audio_info import gather_audio_info

BROKEN = [
    pytest.param("03.flac", b"", id="flac"),
    pytest.param("03.mp3", b"garbage that is not an mp3", id="mp3"),
    pytest.param("03.m4a", b"\x00\x00\x00\x18ftypM4A garbage after it", id="m4a"),
]


def _album_with(tmp_path: Path, name: str, content: bytes) -> str:
    _write_flac(tmp_path / "01.flac")
    (tmp_path / name).write_bytes(content)
    return str(tmp_path)


@pytest.mark.parametrize(("name", "content"), BROKEN)
def test_gather_audio_info_names_a_track_mutagen_cannot_read(tmp_path, name: str, content: bytes) -> None:
    album = _album_with(tmp_path, name, content)

    with pytest.raises(UploadError, match=rf"Could not read audio file: {re.escape(name)} \(.+\)"):
        gather_audio_info(album)


def test_a_cross_upload_reads_the_audio_first_so_a_broken_track_is_named(tmp_path) -> None:
    release = SimpleNamespace(path=_album_with(tmp_path, "03.flac", b""), track_data=None)

    with pytest.raises(UploadError, match=r"Could not read audio file: 03\.flac"):
        _track_data(cast("Any", release))


def test_as_before_gather_audio_info_names_a_track_of_no_type_mutagen_knows(tmp_path, monkeypatch) -> None:
    _write_flac(tmp_path / "01.flac")
    monkeypatch.setattr(audio_info_module, "MutagenFile", lambda _path: None)

    with pytest.raises(UploadError, match=r"^Could not read audio file: 01\.flac$"):
        gather_audio_info(str(tmp_path))
