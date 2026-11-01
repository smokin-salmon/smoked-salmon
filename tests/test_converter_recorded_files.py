"""A conversion's record lists the audio files salmon wrote, under the names they have now.

The converters reuse their own output by that list, whatever the folder check made of its names, refuse a recorded
folder that lost a file, and only ever remake a transcode salmon never recorded: a recorded one may be seeding.
"""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import asyncclick as click
import pytest
from mutagen.flac import FLAC, Picture
from test_uploader_shortened_conversions import _write_flac  # pyright: ignore[reportMissingImports]

import salmon.uploader as uploader
from salmon import cfg, interaction
from salmon.converter import conversions
from salmon.converter import downconverting as dc
from salmon.converter import transcoding as tc
from salmon.errors import NoncompliantFolderStructure, UploadError
from salmon.tagger import folderstructure, renames
from salmon.tagger.retagger import rename_files
from salmon.trackers.ops import OpsApi
from salmon.uploader.staging import staged_source

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
    real_link, calls = os.link, []

    def link(old: str, new: str) -> None:
        calls.append(old)
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied", old)
        real_link(old, new)

    # A file is renamed by a hard link.
    with monkeypatch.context() as patched:
        patched.setattr(renames.os, "link", link)
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
        "salmon.tagger.retagger.generate_file_name", lambda tags, ext, _multi, **_: f"0{tags['tracknumber']}{ext}"
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

    again = _transcode(album, tmp_path)

    assert again == out
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


def test_a_transcode_without_a_record_missing_a_file_is_made_again(made, tmp_path, monkeypatch, capsys) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    os.remove(conversions._sidecar(str(out)))
    (out / "02. two.mp3").unlink()
    made.clear()
    # A folder salmon has no record of is deleted only once the user says so.
    asked: list[str] = []
    monkeypatch.setattr(click, "confirm", lambda text, **_kwargs: asked.append(text) or True)

    again = _transcode(album, tmp_path)

    assert len(asked) == 1
    assert again == out
    assert sorted(made) == ["01. " + LONG + ".mp3", "02. two.mp3"]
    assert (out / "02. two.mp3").is_file()
    assert "exists but appears incomplete, re-transcoding" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("assume_defaults", "answer", "refusal"),
    [(True, None, "with -yyy"), (False, False, "Not overwriting existing folder"), (False, True, None)],
    ids=["yyy_takes_the_default_no", "told_no", "told_yes"],
)
def test_a_folder_that_is_not_the_expected_transcode_goes_only_when_the_user_says_so(
    made, tmp_path, monkeypatch, assume_defaults: bool, answer: bool | None, refusal: str | None
) -> None:
    album = _album(tmp_path)
    existing = Path(tc._build_output_path(str(album), "V0", str(tmp_path)))
    existing.mkdir(parents=True)
    # Not salmon's: e.g. an upload made with another file template, which the user may be seeding.
    (existing / "01 - seeding.mp3").write_bytes(b"seeded")
    asked: list[str] = []

    def confirm(text: str, **_kwargs: Any) -> bool | None:
        asked.append(click.unstyle(text))
        return answer

    monkeypatch.setattr(click, "confirm", confirm)

    async def run() -> str:
        with interaction.assuming_defaults(assume_defaults):
            return await tc.transcode_folder(str(album), "V0", output_dir=str(tmp_path))

    if refusal is None:
        anyio.run(run)
        assert not (existing / "01 - seeding.mp3").exists()
    else:
        with pytest.raises(UploadError, match=refusal):
            anyio.run(run)
        assert (existing / "01 - seeding.mp3").read_bytes() == b"seeded"
    assert len(asked) == (0 if assume_defaults else 1)


def test_a_folder_is_never_removed_for_a_transcode_that_would_write_nothing(made, tmp_path, monkeypatch) -> None:
    album = _album(tmp_path)
    existing = Path(tc._build_output_path(str(album), "V0", str(tmp_path)))
    existing.mkdir(parents=True)
    (existing / "01 - seeding.mp3").write_bytes(b"seeded")
    monkeypatch.setattr(tc, "_collect_transcode_items", lambda _path, _new_path: [])
    # Whatever the user would answer: nothing would take the folder's place.
    monkeypatch.setattr(click, "confirm", lambda _text, **_kwargs: True)

    with pytest.raises(UploadError, match="no expected MP3 output"):
        _transcode(album, tmp_path)

    assert (existing / "01 - seeding.mp3").read_bytes() == b"seeded"


def test_an_old_record_is_reused_by_its_names(made, tmp_path, capsys) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    _without_files(out)
    before, made_before = _snapshot(out), list(made)

    again = _transcode(album, tmp_path)

    assert again == out
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


@pytest.mark.parametrize(
    ("change", "holds"),
    [
        ({"source": "other"}, "a conversion of another album ({other})"),
        ({"bitrate": "320"}, "a transcode of this album at 320"),
        ({"kind": "downconvert", "bit_depth": 16, "sample_rate": 44100}, "a downconversion of this album"),
    ],
    ids=["another source", "another bitrate", "another kind"],
)
def test_another_conversions_record_is_refused_whatever_names_it_holds(made, tmp_path, change, holds) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    other = str(tmp_path / "other")
    sidecar = Path(conversions._sidecar(str(out)))
    record = json.loads(sidecar.read_text())
    change = {key: other if value == "other" else value for key, value in change.items()}
    sidecar.write_text(json.dumps({**record, **change}))
    # Every name is the one this transcode writes: only the record says it is another conversion.
    before = _snapshot(out)

    with pytest.raises(UploadError) as refused:
        _transcode(album, tmp_path)

    assert str(refused.value) == (
        f"{out} holds {holds.format(other=other)}, not this transcode: remove it and run again."
    )
    assert _snapshot(out) == before


# Downconversions


def test_a_shortened_downconversion_is_reused(made, tmp_path, capsys) -> None:
    album = _album(tmp_path, 24, 96000)
    out = _downconvert(album, tmp_path)
    _shorten(out)
    before, made_before = _snapshot(out), list(made)

    again = _downconvert(album, tmp_path)

    assert again == out
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


def _no_record_writes(monkeypatch) -> None:
    def full_disk(*_args, **_kwargs) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(conversions, "record_conversion", full_disk)


def test_an_undone_rename_of_the_tracks_leaves_the_record_as_it_was(made, tmp_path, monkeypatch) -> None:
    # A stopped rename_files batch is put back: the record must follow the undo too.
    out = _transcode(_album(tmp_path), tmp_path)
    before = _files(out)
    assert before is not None
    tags = {
        os.path.join("CD1", f"01. {LONG}.mp3"): {"tracknumber": "1", "title": "One"},
        "02. two.mp3": {"tracknumber": "2", "title": "Two"},
    }
    monkeypatch.setattr(
        "salmon.tagger.retagger.generate_file_name", lambda tags, ext, _multi, **_kw: f"0{tags['tracknumber']}{ext}"
    )
    metadata = {"tracks": {"1": {"1": {"artists": [("A", "main")]}, "2": {"artists": [("A", "main")]}}}}
    real_link, calls = os.link, []

    def link(old: str, new: str) -> None:
        calls.append(old)
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied", old)
        real_link(old, new)

    monkeypatch.setattr(renames.os, "link", link)

    with pytest.raises(UploadError):
        anyio.run(lambda: rename_files(str(out), tags, metadata, auto_rename=True, spectral_ids=None, source="WEB"))

    assert len(calls) == 3
    assert _files(out) == before
    assert all((out / path).is_file() for path in before.values())


def test_a_record_that_cannot_follow_the_renamed_tracks_puts_them_back(made, tmp_path, monkeypatch) -> None:
    # The record is written once the tracks are renamed, and a failed write puts every rename back.
    out = _transcode(_album(tmp_path), tmp_path)
    before = _snapshot(out)
    tags = {
        os.path.join("CD1", f"01. {LONG}.mp3"): {"tracknumber": "1", "title": "One"},
        "02. two.mp3": {"tracknumber": "2", "title": "Two"},
    }
    monkeypatch.setattr(
        "salmon.tagger.retagger.generate_file_name", lambda tags, ext, _multi, **_kw: f"0{tags['tracknumber']}{ext}"
    )
    metadata = {"tracks": {"1": {"1": {"artists": [("A", "main")]}, "2": {"artists": [("A", "main")]}}}}
    _no_record_writes(monkeypatch)

    with pytest.raises(UploadError, match="No space left on device"):
        anyio.run(lambda: rename_files(str(out), tags, metadata, auto_rename=True, spectral_ids=None, source="WEB"))

    assert _snapshot(out) == before


def test_an_undo_that_stops_leaves_a_record_of_what_is_on_disk(made, tmp_path, monkeypatch) -> None:
    out = _transcode(_album(tmp_path), tmp_path)
    tags = {
        os.path.join("CD1", f"01. {LONG}.mp3"): {"tracknumber": "1", "title": "One"},
        "02. two.mp3": {"tracknumber": "2", "title": "Two"},
    }
    monkeypatch.setattr(
        "salmon.tagger.retagger.generate_file_name", lambda tags, ext, _multi, **_kw: f"0{tags['tracknumber']}{ext}"
    )
    metadata = {"tracks": {"1": {"1": {"artists": [("A", "main")]}, "2": {"artists": [("A", "main")]}}}}
    real_link, calls = os.link, []

    def link(old: str, new: str) -> None:
        calls.append(old)
        # The first rename is made, the second fails, and putting the first back fails too.
        if len(calls) in (2, 3):
            raise PermissionError(13, "Permission denied", old)
        real_link(old, new)

    monkeypatch.setattr(renames.os, "link", link)

    with pytest.raises(UploadError, match="partly renamed"):
        anyio.run(lambda: rename_files(str(out), tags, metadata, auto_rename=True, spectral_ids=None, source="WEB"))

    files = _files(out)
    assert files is not None
    assert set(files.values()) == {str(path.relative_to(out)) for path in out.rglob("*.mp3")}
    assert "01.mp3" in files.values()


def test_a_track_with_an_uppercase_extension_is_transcoded_too(made, tmp_path) -> None:
    album = _album(tmp_path)
    _write_flac(album / "03. three.FLAC", 44100, 16, title="Three")

    out = _transcode(album, tmp_path)

    assert (out / "03. three.mp3").is_file()
    assert "03. three.mp3" in made
    assert not [p for p in out.rglob("*") if p.suffix.lower() == ".flac"]


@pytest.mark.parametrize("change", ["gained", "lost"])
def test_a_transcode_of_the_album_before_it_gained_or_lost_a_track_is_refused_not_removed(
    made, tmp_path, change
) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    if change == "gained":
        _write_flac(album / "03. three.flac", 44100, 16, title="Three")
    else:
        (album / "02. two.flac").unlink()
    before = _snapshot(out)

    with pytest.raises(UploadError) as refused:
        _transcode(album, tmp_path)

    assert str(refused.value) == (
        f"{out} holds a transcode of this album from before its files changed, not this transcode: "
        "remove it and run again."
    )
    assert _snapshot(out) == before


@pytest.mark.parametrize("change", ["gained", "lost"])
def test_a_downconversion_of_the_album_before_it_gained_or_lost_a_track_is_refused(made, tmp_path, change) -> None:
    album = _album(tmp_path, 24, 96000)
    out = _downconvert(album, tmp_path)
    if change == "gained":
        _write_flac(album / "03. three.flac", 96000, 24, title="Three")
    else:
        (album / "02. two.flac").unlink()
    before = _snapshot(out)

    with pytest.raises(UploadError, match="incomplete or not salmon's conversion"):
        _downconvert(album, tmp_path)

    assert _snapshot(out) == before


def test_a_downconversion_of_the_album_before_it_gained_a_16_bit_track_is_refused(made, tmp_path) -> None:
    # The 16-bit track is copied as it is, so only the record's list of the album's files knows it is new.
    album = tmp_path / "source" / "Mixed [WEB FLAC]"
    album.mkdir(parents=True)
    _write_flac(album / "01. one.flac", 96000, 24, title="One")
    _write_flac(album / "02. two.flac", 44100, 16, title="Two")
    out = _downconvert(album, tmp_path)
    _write_flac(album / "03. three.flac", 44100, 16, title="Three")
    before = _snapshot(out)

    with pytest.raises(UploadError, match="incomplete or not salmon's conversion"):
        _downconvert(album, tmp_path)

    assert _snapshot(out) == before


def test_a_downconversion_of_the_album_before_it_lost_its_last_24_bit_track_is_refused(made, tmp_path) -> None:
    # Nothing is left to convert, so only the record shows the folder still holds the track the album lost.
    album = tmp_path / "source" / "Mixed [WEB FLAC]"
    album.mkdir(parents=True)
    _write_flac(album / "01. one.flac", 96000, 24, title="One")
    _write_flac(album / "02. two.flac", 44100, 16, title="Two")
    out = _downconvert(album, tmp_path)
    (album / "01. one.flac").unlink()
    before = _snapshot(out)

    with pytest.raises(UploadError, match="incomplete or not salmon's conversion"):
        _downconvert(album, tmp_path)

    assert _snapshot(out) == before


def test_a_transcode_made_from_a_runs_copy_is_reused_from_a_later_runs_copy(made, tmp_path, monkeypatch) -> None:
    # A run that works on a copy (--skip-flac-upload, a protected album kept unrenamed) makes a new one each time.
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    album = _album(tmp_path)
    with staged_source(str(album), scratch=True) as (copy, _rename_into):
        out = _transcode(Path(copy), downloads)
    made.clear()

    with staged_source(str(album), scratch=True) as (copy, _rename_into):
        again = _transcode(Path(copy), downloads)

    assert again == out
    assert made == []
    record = conversions.conversion_of(str(out))
    assert record is not None
    assert record["source"] == str(album)


def test_a_downconversion_made_from_a_runs_copy_is_reused_from_a_later_runs_copy(made, tmp_path, monkeypatch) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    album = _album(tmp_path, 24, 96000)
    with staged_source(str(album), scratch=True) as (copy, _rename_into):
        out = _downconvert(Path(copy), downloads)
    made.clear()

    with staged_source(str(album), scratch=True) as (copy, _rename_into):
        again = _downconvert(Path(copy), downloads)

    assert again == out
    assert made == []
    record = conversions.conversion_of(str(out))
    assert record is not None
    assert record["source"] == str(album)


def _set_md5(path: Path, md5: bytes) -> None:
    """Write md5 into the file's STREAMINFO, the first block: its last 16 bytes are the MD5 of the decoded audio."""
    data = bytearray(path.read_bytes())
    data[26:42] = md5
    path.write_bytes(bytes(data))


def _album_with_md5(tmp_path: Path, md5: str) -> Path:
    """_album, its tracks' audio MD5 set or, as an encoder may leave it, unset."""
    album = _album(tmp_path)
    if md5 == "set":
        for track in album.rglob("*.flac"):
            _set_md5(track, bytes(range(16)))
    return album


def _change(path: Path, change: str) -> None:
    if change == "audio":
        # Other audio: with an MD5 its MD5 differs, and without one the file's bytes do.
        if FLAC(path).info.md5_signature:
            _set_md5(path, bytes(range(1, 17)))
        else:
            with path.open("ab") as fh:
                fh.write(b"other audio")
        return
    tagged = FLAC(path)
    if change == "tag":
        tagged["title"] = "Two (fixed)"
    else:
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, "image/jpeg", b"new cover"
        tagged.add_picture(picture)
    tagged.save()


@pytest.mark.parametrize("md5", ["set", "unset"])
@pytest.mark.parametrize("change", ["tag", "picture", "audio"])
def test_a_transcode_of_the_tracks_before_a_retag_is_refused_not_reused(made, tmp_path, change: str, md5: str) -> None:
    # Its MP3s carry the old tags or pictures: an upload of it would not match the album as it is now.
    album = _album_with_md5(tmp_path, md5)
    out = _transcode(album, tmp_path)
    _change(album / "02. two.flac", change)
    before = _snapshot(out)

    with pytest.raises(UploadError) as refused:
        _transcode(album, tmp_path)

    assert str(refused.value) == (
        f"{out} holds a transcode of this album from before one of its tracks changed or was renamed, "
        "not this transcode: remove it and run again."
    )
    assert _snapshot(out) == before


@pytest.mark.parametrize("md5", ["set", "unset"])
def test_a_transcode_is_reused_when_its_tracks_were_only_touched(made, tmp_path, md5: str) -> None:
    album = _album_with_md5(tmp_path, md5)
    out = _transcode(album, tmp_path)
    written = list(made)
    for track in album.rglob("*.flac"):
        os.utime(track, ns=(1, 1))

    reused = _transcode(album, tmp_path)

    assert reused == out
    assert made == written


def test_a_transcode_recorded_before_salmon_fingerprinted_its_tracks_is_reused_as_before(made, tmp_path) -> None:
    album = _album(tmp_path)
    out = _transcode(album, tmp_path)
    sidecar = Path(conversions._sidecar(str(out)))
    record = json.loads(sidecar.read_text())
    del record["source_fingerprints"]
    sidecar.write_text(json.dumps(record))
    _change(album / "02. two.flac", "tag")
    written = list(made)

    reused = _transcode(album, tmp_path)

    assert reused == out
    assert made == written


def test_a_downconversion_of_the_tracks_before_a_retag_is_refused(made, tmp_path) -> None:
    album = _album(tmp_path, 24, 96000)
    out = _downconvert(album, tmp_path)
    _change(album / "02. two.flac", "tag")
    before = _snapshot(out)

    with pytest.raises(UploadError, match="from before one of its tracks changed or was renamed"):
        _downconvert(album, tmp_path)

    assert _snapshot(out) == before


def test_a_track_unreadable_when_fingerprinted_counts_as_changed_once_it_reads(tmp_path) -> None:
    (tmp_path / "01.flac").write_bytes(b"not a flac yet")
    record = {"source_fingerprints": conversions.source_fingerprints(str(tmp_path), ["01.flac"])}
    unchanged = conversions.changed_sources(str(tmp_path), record)
    _write_flac(tmp_path / "01.flac", 44100, 16, title="One")

    changed = conversions.changed_sources(str(tmp_path), record)

    assert record["source_fingerprints"] == {"01.flac": ""}
    assert unchanged == []
    assert changed == ["01.flac"]


def test_a_track_gone_since_it_was_fingerprinted_counts_as_changed(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", 44100, 16, title="One")
    record = {"source_fingerprints": conversions.source_fingerprints(str(tmp_path), ["01.flac"])}
    (tmp_path / "01.flac").unlink()

    changed = conversions.changed_sources(str(tmp_path), record)

    assert changed == ["01.flac"]
