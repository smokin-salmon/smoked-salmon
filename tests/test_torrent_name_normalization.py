"""Torrent file name normalization (issue #431)."""

import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from salmon import cfg
from salmon.config.validations import Upload
from salmon.uploader.upload import generate_torrent

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

COMPOSED_NAME = "Café.flac"
DECOMPOSED_NAME = unicodedata.normalize("NFD", COMPOSED_NAME)


class FakeGazelleApi:
    def __init__(self, dot_torrents_dir: str) -> None:
        self.announce = "https://example.com/announce"
        self.dot_torrents_dir = dot_torrents_dir
        self.site_string = "TEST"


def _make_album(tmp_path: Path) -> Path:
    album = tmp_path / "Album"
    album.mkdir()
    # Written with the decomposed (NFD) form, as macOS file systems hand back names.
    (album / DECOMPOSED_NAME).write_bytes(b"not really flac data")
    return album


def _file_names(t) -> list[str]:
    return [f.parts[-1] for f in t.files]


def test_torrent_name_normalization_nfc(tmp_path: Path) -> None:
    album = _make_album(tmp_path)
    gazelle_site = FakeGazelleApi(str(tmp_path))

    original = cfg.upload.torrent_name_normalization
    try:
        cfg.upload.torrent_name_normalization = "NFC"
        _tpath, t = generate_torrent(cast("BaseGazelleApi", cast("object", gazelle_site)), str(album))
    finally:
        cfg.upload.torrent_name_normalization = original

    names = _file_names(t)
    assert names == [unicodedata.normalize("NFC", DECOMPOSED_NAME)]
    for name in names:
        assert unicodedata.is_normalized("NFC", name)
    # decomposed form must be gone
    assert DECOMPOSED_NAME not in names


def test_torrent_name_normalization_nfd(tmp_path: Path) -> None:
    album = tmp_path / "Album2"
    album.mkdir()
    composed = unicodedata.normalize("NFC", COMPOSED_NAME)
    (album / composed).write_bytes(b"not really flac data")
    gazelle_site = FakeGazelleApi(str(tmp_path))

    original = cfg.upload.torrent_name_normalization
    try:
        cfg.upload.torrent_name_normalization = "NFD"
        _tpath, t = generate_torrent(cast("BaseGazelleApi", cast("object", gazelle_site)), str(album))
    finally:
        cfg.upload.torrent_name_normalization = original

    names = _file_names(t)
    assert names == [unicodedata.normalize("NFD", composed)]
    for name in names:
        assert unicodedata.is_normalized("NFD", name)


def test_torrent_name_normalization_default_leaves_names_untouched(tmp_path: Path) -> None:
    album = _make_album(tmp_path)
    gazelle_site = FakeGazelleApi(str(tmp_path))

    assert cfg.upload.torrent_name_normalization == ""
    _tpath, t = generate_torrent(cast("BaseGazelleApi", cast("object", gazelle_site)), str(album))

    names = _file_names(t)
    assert names == [DECOMPOSED_NAME]


def test_torrent_name_normalization_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="torrent_name_normalization"):
        Upload(torrent_name_normalization="nfc")
