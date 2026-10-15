"""Torrent file name normalization (issue #431)."""

import os
import shutil
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from torf import Torrent

from salmon import cfg
from salmon.config.validations import Seedbox, Upload
from salmon.errors import UploadRefusedError
from salmon.uploader import seedbox as seedbox_module
from salmon.uploader.seedbox import warn_unmatched_normalization
from salmon.uploader.upload import _normalize_torrent_names, generate_torrent

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

COMPOSED_NAME = "Café.flac"
DECOMPOSED_NAME = unicodedata.normalize("NFD", COMPOSED_NAME)


class FakeGazelleApi:
    def __init__(self, dot_torrents_dir: str) -> None:
        self.announce = "https://example.com/announce"
        self.dot_torrents_dir = dot_torrents_dir
        self.site_string = "TEST"
        self.site_code = "RED"


def _make_album(tmp_path: Path) -> Path:
    album = tmp_path / "Album"
    album.mkdir()
    # Named in the decomposed (NFD) form, so NFC normalization has something to change.
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


def test_a_normalized_torrent_no_longer_reads_the_files_on_disk(tmp_path: Path, monkeypatch) -> None:
    album = _make_album(tmp_path)
    monkeypatch.setattr(cfg.upload, "torrent_name_normalization", "NFC")
    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(tmp_path))))
    _tpath, t = generate_torrent(gazelle_site, str(album))
    shutil.rmtree(album)

    assert t.dump()


def test_a_watch_folder_that_takes_the_torrent_at_once_does_not_break_the_upload(tmp_path: Path, monkeypatch) -> None:
    album = _make_album(tmp_path)
    monkeypatch.setattr(cfg.upload, "torrent_name_normalization", "NFC")
    write = Torrent.write

    def write_then_import(self, filepath, **kwargs) -> None:
        write(self, filepath, **kwargs)
        os.remove(filepath)

    monkeypatch.setattr(Torrent, "write", write_then_import)
    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(tmp_path))))

    _tpath, t = generate_torrent(gazelle_site, str(album))

    assert _file_names(t) == [unicodedata.normalize("NFC", DECOMPOSED_NAME)]


def test_two_files_that_normalize_to_one_path_are_refused() -> None:
    """Built by hand: a normalization-insensitive file system (APFS) cannot hold both names."""
    files = [{"length": 1, "path": ["CD1", COMPOSED_NAME]}, {"length": 1, "path": ["CD1", DECOMPOSED_NAME]}]
    t = SimpleNamespace(metainfo={"info": {"name": "Album", "files": files}})

    with pytest.raises(UploadRefusedError, match="CD1/Café.flac once NFC-normalized"):
        _normalize_torrent_names(cast("Torrent", cast("object", t)), "NFC")


def test_colliding_names_are_refused_before_the_torrent_is_written(tmp_path: Path, monkeypatch) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    (album / COMPOSED_NAME).write_bytes(b"one")
    (album / DECOMPOSED_NAME).write_bytes(b"two")
    if len(list(album.iterdir())) < 2:
        pytest.skip("this file system folds Unicode forms into one name")
    monkeypatch.setattr(cfg.upload, "torrent_name_normalization", "NFC")
    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(tmp_path))))

    with pytest.raises(UploadRefusedError, match="once NFC-normalized"):
        generate_torrent(gazelle_site, str(album))

    assert not list(tmp_path.glob("*.torrent"))


def test_colliding_names_make_a_torrent_as_on_disk_when_normalization_is_off(tmp_path: Path) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    (album / COMPOSED_NAME).write_bytes(b"one")
    (album / DECOMPOSED_NAME).write_bytes(b"two")
    if len(list(album.iterdir())) < 2:
        pytest.skip("this file system folds Unicode forms into one name")
    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(tmp_path))))

    _tpath, t = generate_torrent(gazelle_site, str(album))

    assert sorted(_file_names(t)) == sorted([COMPOSED_NAME, DECOMPOSED_NAME])


def test_the_default_torrent_is_the_one_master_wrote(tmp_path: Path) -> None:
    album = _make_album(tmp_path)
    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(tmp_path))))
    tpath, t = generate_torrent(gazelle_site, str(album))
    assert Path(tpath).read_bytes() == t.dump()


# A seedbox whose client cannot find the normalized names


@pytest.fixture
def seedboxes(monkeypatch: pytest.MonkeyPatch):
    """Configure these seedboxes, on Linux, with none warned about yet."""
    monkeypatch.setattr(seedbox_module, "_warned_normalization", set())
    monkeypatch.setattr(seedbox_module.sys, "platform", "linux")
    monkeypatch.setattr(cfg.upload, "upload_to_seedbox", True)

    def configure(*boxes: Seedbox) -> None:
        monkeypatch.setattr(cfg, "seedbox", list(boxes))

    return configure


def _box(**fields) -> Seedbox:
    return Seedbox(**{"name": "box", "enabled": True, "url": "remote", "type": "rclone", **fields})


@pytest.mark.parametrize(
    ("box", "form", "said"),
    [
        (_box(), "NFC", "without --local-unicode-normalization in its extra_args"),
        (_box(extra_args=["--local-unicode-normalization"]), "NFD", "rclone cannot copy names as NFD"),
        (_box(type="local"), "NFC", "Linux keeps their names as they are"),
    ],
    ids=["rclone-nfc", "rclone-nfd", "local-linux"],
)
def test_a_seedbox_that_cannot_match_the_form_is_warned_about(seedboxes, capsys, box, form, said) -> None:
    seedboxes(box)
    warn_unmatched_normalization(form, "RED")
    out = capsys.readouterr().out
    assert f"torrent_name_normalization is {form}, but on box " in out
    assert said in out


@pytest.mark.parametrize(
    "box",
    [
        _box(extra_args=["--local-unicode-normalization"]),
        _box(extra_args=["--local-unicode-normalization=true"]),
        _box(trackers=["OPS"]),
        _box(enabled=False),
    ],
    ids=["flag", "flag=true", "other-tracker", "disabled"],
)
def test_a_seedbox_that_matches_nfc_or_gets_no_torrent_is_not_warned_about(seedboxes, capsys, box) -> None:
    seedboxes(box)
    warn_unmatched_normalization("NFC", "RED")
    assert "torrent_name_normalization" not in capsys.readouterr().out


def test_a_local_seedbox_on_macos_finds_any_form(seedboxes, capsys, monkeypatch) -> None:
    seedboxes(_box(type="local"))
    monkeypatch.setattr(seedbox_module.sys, "platform", "darwin")
    warn_unmatched_normalization("NFD", "RED")
    assert "torrent_name_normalization" not in capsys.readouterr().out


def test_no_warning_without_seedbox_uploads(seedboxes, capsys, monkeypatch) -> None:
    seedboxes(_box())
    monkeypatch.setattr(cfg.upload, "upload_to_seedbox", False)
    warn_unmatched_normalization("NFC", "RED")
    assert "torrent_name_normalization" not in capsys.readouterr().out


def test_a_seedbox_is_warned_about_once(seedboxes, capsys) -> None:
    seedboxes(_box())
    warn_unmatched_normalization("NFC", "RED")
    warn_unmatched_normalization("NFC", "OPS")
    assert capsys.readouterr().out.count("torrent_name_normalization is NFC") == 1


def test_the_warning_masks_the_seedbox_url(seedboxes, capsys) -> None:
    seedboxes(_box(name="", url="sftp://user:hunter2@host"))
    warn_unmatched_normalization("NFC", "RED")
    assert "hunter2" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("normalization", "normalize", "warned"),
    [("NFC", True, True), ("", True, False), ("NFC", False, False)],
    ids=["normalized", "as-on-disk", "cross-upload"],
)
def test_only_a_normalized_torrent_warns(
    seedboxes, capsys, monkeypatch, tmp_path: Path, normalization: str, normalize: bool, warned: bool
) -> None:
    seedboxes(_box())
    monkeypatch.setattr(cfg.upload, "torrent_name_normalization", normalization)
    album = _make_album(tmp_path)
    gazelle_site = FakeGazelleApi(str(tmp_path))
    generate_torrent(cast("BaseGazelleApi", cast("object", gazelle_site)), str(album), normalize=normalize)
    assert ("torrent_name_normalization is NFC" in capsys.readouterr().out) is warned
