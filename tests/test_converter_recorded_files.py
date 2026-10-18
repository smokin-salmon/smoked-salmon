"""A conversion's record lists the audio files salmon wrote, under the names they have now.

The converters reuse their own output by that list, whatever the folder check made of its names, refuse a recorded
folder that lost a file, and only ever remake a transcode salmon never recorded: a recorded one may be seeding.
"""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest
from test_uploader_shortened_conversions import _write_flac  # pyright: ignore[reportMissingImports]

import salmon.uploader as uploader
from salmon import cfg
from salmon.converter import conversions
from salmon.converter import downconverting as dc
from salmon.converter import transcoding as tc
from salmon.errors import NoncompliantFolderStructure, UploadError
from salmon.tagger import folderstructure, renames
from salmon.tagger.retagger import rename_files
from salmon.trackers.ops import OpsApi

if TYPE_CHECKING:
    from salmon.uploader.seedbox import UploadManager

LONG = "x" * 60
SHORT_LIMIT = 70


@pytest.fixture
def made(monkeypatch) -> list[str]:
    """The real converters, writing files instead of encoding them: each file they write."""
    written: list[str] = []

    async def encode(items: list[Any], bitrate: str) -> None:
        for item in items:
            written.append(os.path.basename(item.dst))
            Path(item.dst).parent.mkdir(parents=True, exist_ok=True)
            Path(item.dst).write_bytes(bitrate.encode() * 100)

    async def sox(items: list[Any], bit_depth: int) -> None:
        for item in items:
            written.append(os.path.basename(item.dst))
            Path(item.dst).parent.mkdir(parents=True, exist_ok=True)
            _write_flac(Path(item.dst), item.target_rate, bit_depth)

    monkeypatch.setattr(tc, "_transcode_audio_files", encode)
    monkeypatch.setattr(dc, "_convert_audio_files", sox)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return written


def _album(tmp_path: Path, bits: int = 16, rate: int = 44100) -> Path:
    """An album folder: a long-named track in a disc folder, a short-named one, and a cover."""
    album = tmp_path / "source" / "Album [WEB FLAC]"
    (album / "CD1").mkdir(parents=True)
    _write_flac(album / "CD1" / f"01. {LONG}.flac", rate, bits, title="One")
    _write_flac(album / "02. two.flac", rate, bits, title="Two")
    (album / "cover.jpg").write_bytes(b"jpeg")
    return album


def _transcode(album: Path, tmp_path: Path) -> Path:
    return Path(anyio.run(lambda: tc.transcode_folder(str(album), "V0", output_dir=str(tmp_path))))


def _downconvert(album: Path, tmp_path: Path) -> Path:
    return Path(anyio.run(lambda: dc.convert_folder(str(album), 16, 44100, output_dir=str(tmp_path)))[1])


def _shorten(folder: Path) -> None:
    """The folder check's truncation, at a limit the long track exceeds."""
    folderstructure._check_path_lengths(str(folder), False, len(folder.name) + SHORT_LIMIT)


def _snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(folder)): (path.read_bytes(), path.stat().st_ino)
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _files(folder: Path) -> dict[str, str] | None:
    record = conversions.conversion_of(str(folder))
    assert record is not None
    return conversions.recorded_files(record)


def _without_files(folder: Path) -> None:
    """Make folder's record one from before salmon listed the files it wrote."""
    sidecar = Path(conversions._sidecar(str(folder)))
    record = json.loads(sidecar.read_text())
    del record["files"]
    sidecar.write_text(json.dumps(record))


# What the record lists


def test_the_record_lists_each_file_written_by_its_source_file(made, tmp_path) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)

    assert _files(out) == {
        os.path.join("CD1", f"01. {LONG}.flac"): os.path.join("CD1", f"01. {LONG}.mp3"),
        "02. two.flac": "02. two.mp3",
    }


def test_the_folder_check_shortens_a_name_in_the_record_too(made, tmp_path) -> None:
    out = _transcode(_album(tmp_path), tmp_path)

    _shorten(out)

    (shortened,) = (out / "CD1").iterdir()
    assert shortened.name.endswith("...mp3")
    assert _files(out) == {
        os.path.join("CD1", f"01. {LONG}.flac"): os.path.join("CD1", shortened.name),
        "02. two.flac": "02. two.mp3",
    }


def test_a_truncation_stopped_partway_leaves_a_record_of_what_is_on_disk(made, tmp_path, monkeypatch) -> None:
    album = _album(tmp_path)
    _write_flac(album / "CD1" / f"03. {LONG}.flac", 44100, 16, title="Three")
    out = _transcode(album, tmp_path)
    real_rename, calls = os.rename, []

    def rename(old: str, new: str) -> None:
        calls.append(old)
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied", old)
        real_rename(old, new)

    with monkeypatch.context() as patched:
        patched.setattr(renames.os, "rename", rename)
        with pytest.raises(NoncompliantFolderStructure):
            _shorten(out)

    files = _files(out)
    assert files is not None
    on_disk = {str(path.relative_to(out)) for path in out.rglob("*.mp3")}
    assert set(files.values()) == on_disk
    # One of the two long names was shortened before the stop, the other was not.
    assert sum(path.endswith("...mp3") for path in on_disk) == 1


def test_a_rename_of_a_sub_folder_moves_its_files_in_the_record(made, tmp_path) -> None:
    out = _transcode(_album(tmp_path), tmp_path)

    os.rename(out / "CD1", out / "Disc 1")
    conversions.note_renamed(str(out), [(str(out / "CD1"), str(out / "Disc 1"))])

    assert _files(out) == {
        os.path.join("CD1", f"01. {LONG}.flac"): os.path.join("Disc 1", f"01. {LONG}.mp3"),
        "02. two.flac": "02. two.mp3",
    }


def test_renaming_the_tracks_of_a_converted_folder_renames_them_in_the_record(made, tmp_path, monkeypatch) -> None:
    out = _transcode(_album(tmp_path), tmp_path)
    tags = {
        os.path.join("CD1", f"01. {LONG}.mp3"): {"tracknumber": "1", "title": "One"},
        "02. two.mp3": {"tracknumber": "2", "title": "Two"},
    }
    monkeypatch.setattr(
        "salmon.tagger.retagger.generate_file_name", lambda tags, ext, _multi: f"0{tags['tracknumber']}{ext}"
    )
    metadata = {"tracks": {"1": {"1": {"artists": [("A", "main")]}, "2": {"artists": [("A", "main")]}}}}

    anyio.run(lambda: rename_files(str(out), tags, metadata, auto_rename=True, spectral_ids=None, source="WEB"))

    files = _files(out)
    assert files is not None
    assert all((out / path).is_file() for path in files.values())
    assert files["02. two.flac"] == "02.mp3"


def test_a_rename_in_a_folder_without_a_record_writes_none(tmp_path) -> None:
    folder = tmp_path / "Album"
    folder.mkdir()

    conversions.note_renamed(str(folder), [(str(folder / "a.flac"), str(folder / "b.flac"))])

    assert not (tmp_path / conversions.REGISTRY_DIR).exists()


# Transcodes


def test_a_shortened_transcode_is_reused(made, tmp_path, capsys) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    _shorten(out)
    before, made_before = _snapshot(out), list(made)

    assert _transcode(album, tmp_path) == out

    assert made == made_before
    assert _snapshot(out) == before
    assert f"{out} already exists." in capsys.readouterr().out


def test_a_recorded_transcode_missing_a_file_is_refused_not_removed(made, tmp_path) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    _shorten(out)
    (out / "02. two.mp3").unlink()
    before = _snapshot(out)

    with pytest.raises(UploadError) as refused:
        _transcode(album, tmp_path)

    assert str(refused.value) == (
        f"{out} is salmon's transcode of this album at V0, but 02. two.mp3 is missing: remove it and run again."
    )
    assert _snapshot(out) == before


def test_a_transcode_without_a_record_missing_a_file_is_made_again(made, tmp_path, capsys) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    os.remove(conversions._sidecar(str(out)))
    (out / "02. two.mp3").unlink()
    made.clear()

    assert _transcode(album, tmp_path) == out

    assert sorted(made) == ["01. " + LONG + ".mp3", "02. two.mp3"]
    assert (out / "02. two.mp3").is_file()
    assert "exists but appears incomplete, re-transcoding" in capsys.readouterr().out


def test_an_old_record_is_reused_by_its_names(made, tmp_path, capsys) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    _without_files(out)
    before, made_before = _snapshot(out), list(made)

    assert _transcode(album, tmp_path) == out

    assert made == made_before
    assert _snapshot(out) == before
    assert f"{out} already exists." in capsys.readouterr().out


def test_an_old_record_whose_names_differ_is_refused_not_removed(made, tmp_path) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    _without_files(out)
    os.rename(out / "02. two.mp3", out / "02. tw..mp3")
    before = _snapshot(out)

    with pytest.raises(UploadError) as refused:
        _transcode(album, tmp_path)

    assert str(refused.value) == (
        f"{out} is incomplete or not salmon's transcode of this album at V0: remove it and run again."
    )
    assert _snapshot(out) == before


def test_another_albums_transcode_under_the_name_is_refused_not_removed(made, tmp_path) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    record = json.loads(Path(conversions._sidecar(str(out))).read_text())
    Path(conversions._sidecar(str(out))).write_text(json.dumps({**record, "source": str(tmp_path / "other")}))
    os.rename(out / "02. two.mp3", out / "02. tw..mp3")
    before = _snapshot(out)

    with pytest.raises(UploadError):
        _transcode(album, tmp_path)

    assert _snapshot(out) == before


# Downconversions


def test_a_shortened_downconversion_is_reused(made, tmp_path, capsys) -> None:
    album = _album(tmp_path, 24, 96000)
    out = _downconvert(album, tmp_path)
    _shorten(out)
    before, made_before = _snapshot(out), list(made)

    assert _downconvert(album, tmp_path) == out

    assert made == made_before
    assert _snapshot(out) == before
    assert f"{out} already exists." in capsys.readouterr().out


def test_a_recorded_downconversion_missing_a_file_is_refused_naming_it(made, tmp_path) -> None:
    album = _album(tmp_path, 24, 96000)
    out = _downconvert(album, tmp_path)
    _shorten(out)
    (shortened,) = (out / "CD1").iterdir()
    shortened.unlink()
    (out / "02. two.flac").unlink()
    before = _snapshot(out)

    with pytest.raises(UploadError) as refused:
        _downconvert(album, tmp_path)

    missing = f"02. two.flac, {os.path.join('CD1', shortened.name)}"
    assert str(refused.value) == (
        f"{out} is salmon's conversion of this album at these settings, but {missing} are missing: "
        "remove it and run again."
    )
    assert _snapshot(out) == before


def test_an_old_downconversion_record_whose_names_differ_is_refused(made, tmp_path) -> None:
    album = _album(tmp_path, 24, 96000)
    out = _downconvert(album, tmp_path)
    _without_files(out)
    _shorten(out)

    with pytest.raises(UploadError) as refused:
        _downconvert(album, tmp_path)

    assert "is incomplete or not salmon's conversion of this album at these settings" in str(refused.value)


# The upload of a reused conversion


def test_a_shortened_downconversion_reads_by_its_source_names(made, tmp_path) -> None:
    out = _downconvert(_album(tmp_path, 24, 96000), tmp_path)
    _shorten(out)
    (shortened,) = (out / "CD1").iterdir()
    long_name = os.path.join("CD1", f"01. {LONG}.flac")
    on_disk = {os.path.join("CD1", shortened.name): {"track": 1}, "02. two.flac": {"track": 2}}

    assert uploader._by_source_name(str(out), on_disk) == {long_name: {"track": 1}, "02. two.flac": {"track": 2}}


def test_a_folder_without_a_record_reads_by_its_names(tmp_path) -> None:
    info = {"01.flac": {"precision": 16}}

    assert uploader._by_source_name(str(tmp_path / "Album"), info) == info


def test_a_refused_transcode_is_skipped_and_the_next_task_runs(monkeypatch, capsys) -> None:
    made: list[str] = []

    async def transcode_folder(path: str, bitrate: str, output_dir: str | None = None) -> str:
        if bitrate == "320":
            raise UploadError("/out [320] is salmon's transcode of this album at 320, but 01.mp3 is missing.")
        made.append(bitrate)
        return f"/out [{bitrate}]"

    async def upload_and_report(_site, path, *_args, **_kwargs):
        made.append(f"up {path}")
        return 1, 5, "", None, "https://tracker.test/t"

    async def check(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(uploader, "transcode_folder", transcode_folder)
    monkeypatch.setattr(uploader, "upload_and_report", upload_and_report)
    monkeypatch.setattr(uploader, "check_folder_structure", check)
    monkeypatch.setattr(uploader, "generate_transcode_description", lambda *_a, **_k: "desc")
    tasks = [
        {"name": "MP3 320", "action": "transcode", "encoding": "320"},
        {"name": "MP3 V0", "action": "transcode", "encoding": "V0"},
    ]
    anyio.run(
        lambda: uploader.execute_downconversion_tasks(
            tasks,
            "/release",
            OpsApi(),
            5,
            {"scene": False},
            None,
            {},
            False,
            False,
            None,
            None,
            None,
            None,
            None,
            cast("UploadManager", cast("object", None)),
            "WEB",
            "https://tracker.test/torrents.php?torrentid=1",
        )
    )

    assert made == ["V0", "up /out [V0]"]
    assert "01.mp3 is missing" in capsys.readouterr().out
