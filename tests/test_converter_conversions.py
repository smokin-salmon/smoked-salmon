"""A converted folder remembers where it came from, so uploading it later describes the conversion."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    RENAMED,
    _album,
    _run_up,
    _torrent_lines,
    image_uploads,  # noqa: F401 (a fixture: see the tests that use it)
)

import salmon.uploader as uploader
from salmon import cfg, dryrun
from salmon.converter import conversions
from salmon.converter import downconverting as dc
from salmon.converter import transcoding as tc
from salmon.errors import UploadError
from salmon.tagger import foldername
from salmon.uploader.upload import generate_t_description

DOWNCONVERT = {"source": "/x/src", "kind": "downconvert", "bit_depth": 16, "sample_rate": 44100}


def _facts(folder: str) -> dict | None:
    """What a folder's record says, without the name it is keyed by."""
    data = conversions.conversion_of(folder)
    return None if data is None else {key: value for key, value in data.items() if key != "output"}


def _write_record(folder, entry: dict) -> None:
    """Write a record by hand, for the folder name it is keyed by."""
    sidecar = conversions._sidecar(str(folder))
    os.makedirs(os.path.dirname(sidecar), exist_ok=True)
    with open(sidecar, "w", encoding="utf-8") as fh:
        json.dump({"output": folder.name, **entry}, fh)


def test_record_and_lookup_roundtrip(tmp_path) -> None:
    out = tmp_path / "Artist - Album (2022) [WEB FLAC]"
    out.mkdir()

    conversions.record_conversion(str(out), **DOWNCONVERT)

    assert _facts(str(out)) == DOWNCONVERT
    assert conversions.conversion_of(str(tmp_path / "unrelated")) is None
    assert (tmp_path / conversions.REGISTRY_DIR / f"{out.name}.json").exists()
    assert not any(out.iterdir()), "nothing lands inside the album"


def test_two_folders_under_one_parent_keep_both_records(tmp_path) -> None:
    first, second = tmp_path / "A [WEB FLAC]", tmp_path / "B [WEB FLAC]"

    conversions.record_conversion(str(first), **DOWNCONVERT)
    conversions.record_conversion(str(second), source="/x", kind="transcode", bitrate="V0")
    conversions.record_conversion(str(first), **{**DOWNCONVERT, "sample_rate": 48000})

    assert _facts(str(first)) == {**DOWNCONVERT, "sample_rate": 48000}
    assert _facts(str(second)) == {"source": "/x", "kind": "transcode", "bitrate": "V0"}
    assert not list((tmp_path / conversions.REGISTRY_DIR).glob("*.tmp"))


def test_a_corrupt_sidecar_is_ignored_and_replaced(tmp_path) -> None:
    out = tmp_path / "Album [WEB FLAC]"
    (tmp_path / conversions.REGISTRY_DIR).mkdir()
    (tmp_path / conversions.REGISTRY_DIR / f"{out.name}.json").write_text("{not json")

    assert conversions.conversion_of(str(out)) is None
    conversions.record_conversion(str(out), source="/x", kind="transcode", bitrate="V0")
    assert _facts(str(out)) == {"source": "/x", "kind": "transcode", "bitrate": "V0"}


@pytest.mark.parametrize(
    "entry",
    [
        ["not", "a", "mapping"],
        {"source": "/x", "kind": "upsample", "bit_depth": 16, "sample_rate": 44100},
        {"kind": "transcode", "bitrate": "V0"},
        {"source": "/x", "kind": "transcode", "bitrate": "V9"},
        {"source": "/x", "kind": "downconvert", "bit_depth": 32, "sample_rate": 44100},
        {"source": "/x", "kind": "downconvert", "bit_depth": 16, "sample_rate": "44100"},
        {"source": "/x", "kind": "downconvert", "bit_depth": 16, "sample_rate": []},
        {"source": "/x", "kind": ["transcode"], "bitrate": "V0"},
        {"source": "/x", "kind": "transcode", "bitrate": ["V0"]},
        {"source": "/x", "kind": "downconvert", "bit_depth": [16], "sample_rate": 44100},
        {"source": "/x", "kind": {"a": 1}, "bit_depth": 16, "sample_rate": 44100},
    ],
)
def test_an_unusable_entry_reads_as_no_conversion(tmp_path, entry) -> None:
    out = tmp_path / "Album [WEB FLAC]"
    _write_record(out, entry if isinstance(entry, dict) else {})
    if not isinstance(entry, dict):
        with open(conversions._sidecar(str(out)), "w", encoding="utf-8") as fh:
            json.dump(entry, fh)

    assert conversions.conversion_of(str(out)) is None


def test_a_record_naming_another_folder_is_ignored_with_one_line(tmp_path, capsys) -> None:
    out, other = tmp_path / "Album [WEB FLAC]", tmp_path / "Other [WEB FLAC]"
    conversions.record_conversion(str(other), **DOWNCONVERT)
    os.replace(conversions._sidecar(str(other)), conversions._sidecar(str(out)))

    assert conversions.conversion_of(str(out)) is None
    assert capsys.readouterr().out.count("Ignoring") == 1


def test_a_missing_record_reads_as_no_conversion_quietly(tmp_path, capsys) -> None:
    assert conversions.conversion_of(str(tmp_path / "Album [WEB FLAC]")) is None
    assert capsys.readouterr().out == ""


def _stub_convert(monkeypatch, out, items):
    async def no_conversion(_items, _bit_depth):
        return None

    monkeypatch.setattr(dc, "_validate_lossless", lambda _path: None)
    monkeypatch.setattr(dc, "_build_output_path", lambda *_a: str(out))
    monkeypatch.setattr(dc, "_collect_convert_items", lambda *_a: items)
    monkeypatch.setattr(dc, "_copy_extra_files", lambda *_a, **_k: None)
    monkeypatch.setattr(dc, "_convert_audio_files", no_conversion)


def test_convert_folder_records_its_output(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB 24bit FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB FLAC]"
    _stub_convert(monkeypatch, out, [SimpleNamespace(src="01.flac", target_rate=44100)])

    rate, path = anyio.run(dc.convert_folder, str(src), 16, 44100)

    assert (rate, path) == (44100, str(out))
    assert _facts(str(out)) == {**DOWNCONVERT, "source": str(src)}


def test_a_mixed_family_folder_records_every_target_rate(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB 24bit FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB FLAC]"
    items = [SimpleNamespace(src="01.flac", target_rate=48000), SimpleNamespace(src="02.flac", target_rate=44100)]
    _stub_convert(monkeypatch, out, items)

    anyio.run(dc.convert_folder, str(src), 16, None)

    assert _facts(str(out)) == {**DOWNCONVERT, "source": str(src), "sample_rate": [44100, 48000]}


def test_nothing_converted_means_nothing_recorded(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB FLAC] (copy)"
    _stub_convert(monkeypatch, out, [])

    anyio.run(dc.convert_folder, str(src), 16, 44100)

    assert conversions.conversion_of(str(out)) is None


def _stub_transcode(monkeypatch, out, items):
    async def no_transcode(_items, _bitrate):
        return None

    monkeypatch.setattr(tc, "_validate_lossless", lambda _path: None)
    monkeypatch.setattr(tc, "_build_output_path", lambda *_a: str(out))
    monkeypatch.setattr(tc, "_collect_transcode_items", lambda *_a: items)
    monkeypatch.setattr(tc, "_copy_extra_files", lambda *_a, **_k: None)
    monkeypatch.setattr(tc, "_transcode_audio_files", no_transcode)


def test_transcode_folder_records_its_output(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB MP3 V0]"
    _stub_transcode(monkeypatch, out, [SimpleNamespace(src="01.flac")])

    result = anyio.run(tc.transcode_folder, str(src), "V0")

    assert result == str(out)
    assert _facts(str(out)) == {"source": str(src), "kind": "transcode", "bitrate": "V0"}


def test_transcode_of_nothing_records_nothing(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB MP3 V0]"
    _stub_transcode(monkeypatch, out, [])

    anyio.run(tc.transcode_folder, str(src), "V0")

    assert conversions.conversion_of(str(out)) is None


def test_conversion_note_is_the_in_run_description_without_specifics_and_footer() -> None:
    url = "https://redacted.sh/torrents.php?id=2855221"
    transcode = {"source": "/x", "kind": "transcode", "bitrate": "V0"}
    footer = "[hr]Uploaded with"

    note = uploader.converted_from_note(DOWNCONVERT, url)
    assert note is not None and note in dc.generate_conversion_description(url, 44100, 16)
    assert footer not in note and "Encode Specifics" not in note
    assert uploader.converted_from_note(transcode, url) == tc.transcode_note(url, "V0")
    assert tc.generate_transcode_description(url, "V0").startswith(tc.transcode_note(url, "V0") + footer)
    assert uploader.converted_from_note(None, url) is None


def test_the_conversion_note_goes_before_the_footer_and_changes_nothing_else() -> None:
    args = {
        "metadata": {"date": "2025-07-25", "urls": []},
        "track_data": {"01. One.flac": {"duration": 60, "bit rate": 0, "precision": 16, "sample rate": 44100}},
        "hybrid": False,
        "metadata_urls": [],
        "spectral_urls": None,
        "spectral_ids": None,
        "lossy_comment": None,
        "source_url": None,
    }
    plain = generate_t_description(**args)
    note = tc.transcode_note("https://tracker.test/torrents.php?id=1", "V0")

    assert generate_t_description(**args, conversion_note=None) == plain
    with_note = generate_t_description(**args, conversion_note=note)
    assert with_note == plain.replace("[hr]Uploaded", note + "[hr]Uploaded")
    assert with_note.count("Uploaded with") == 1


def test_a_mixed_family_description_lists_one_sox_command_per_rate() -> None:
    description = dc.generate_conversion_description("https://redacted.sh/torrents.php?id=1", [44100, 48000], 16)

    assert "16 bit 44.1 / 48.0 kHz" in description
    assert description.count("sox input.flac") == 2
    assert "rate -v -L 44100 dither\nsox" in description and "rate -v -L 48000 dither" in description


def test_carry_conversion_follows_a_moved_folder(tmp_path) -> None:
    old, new = tmp_path / "old name", tmp_path / "new name"
    conversions.record_conversion(str(old), **DOWNCONVERT)

    conversions.carry_conversion(str(old), str(new))

    assert _facts(str(new)) == DOWNCONVERT
    assert conversions.conversion_of(str(old)) is None, "the old folder is gone, so its record is too"


def test_carry_conversion_keeps_the_record_while_the_old_folder_remains(tmp_path) -> None:
    old, new = tmp_path / "old name", tmp_path / "new name"
    old.mkdir()
    conversions.record_conversion(str(old), **DOWNCONVERT)

    conversions.carry_conversion(str(old), str(new))
    conversions.carry_conversion(str(old), str(old))

    assert _facts(str(new)) == DOWNCONVERT
    assert _facts(str(old)) == DOWNCONVERT


def test_a_renamed_folder_can_still_be_uploaded_with_its_note(tmp_path, monkeypatch) -> None:
    # The upload reads the record, renames the folder, and may abort; the retry must find the record again.
    monkeypatch.setattr(foldername.cfg.directory, "download_directory", str(tmp_path))
    monkeypatch.setattr(foldername.cfg.upload.formatting, "remove_source_dir", True)
    template = "{artists} - {title} ({year}) [{source} {format}]"
    monkeypatch.setattr(foldername.cfg.upload.formatting, "folder_template", template)
    album = tmp_path / "(2022) journaling"
    album.mkdir()
    (album / "01.flac").write_bytes(b"x")
    conversions.record_conversion(str(album), **DOWNCONVERT)
    metadata = {
        "artists": [("Illy", "main")],
        "title": "journaling",
        "year": 2022,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
    }

    renamed = foldername.rename_folder(str(album), metadata, auto_rename=True, check=False)

    assert renamed == str(tmp_path / "Illy - journaling (2022) [WEB FLAC]")
    assert _facts(renamed) == DOWNCONVERT
    assert conversions.conversion_of(str(album)) is None


def test_an_unreadable_sidecar_is_not_read_as_no_conversion(tmp_path, monkeypatch) -> None:
    out = tmp_path / "Album [WEB FLAC]"
    conversions.record_conversion(str(out), **DOWNCONVERT)

    def denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("builtins.open", denied)

    with pytest.raises(PermissionError):
        conversions.conversion_of(str(out))


def test_staging_a_library_album_takes_its_record_along(tmp_path, monkeypatch) -> None:
    from salmon.uploader.staging import staged_source

    library, downloads = tmp_path / "library", tmp_path / "downloads"
    album = library / "Illy" / "journaling [WEB FLAC]"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"x")
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    # The library never gets a record of its own: this one is as a user left it there.
    sidecar = conversions._sidecar(str(album))
    os.makedirs(os.path.dirname(sidecar))
    with open(sidecar, "w", encoding="utf-8") as fh:
        json.dump({**DOWNCONVERT, "output": album.name}, fh)

    with staged_source(str(album), scratch=False) as (staged, _rename_into):
        assert _facts(staged) == DOWNCONVERT
    assert _facts(str(album)) == DOWNCONVERT, "the library album keeps its own record"


# What protects the library, and what a dry run may write


@pytest.fixture
def library_and_downloads(monkeypatch, tmp_path) -> tuple[Path, Path]:
    library, downloads = tmp_path / "library", tmp_path / "downloads"
    library.mkdir()
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    return library, downloads


def _records_under(folder: Path) -> list[Path]:
    return list(folder.rglob(conversions.REGISTRY_DIR))


def test_nothing_is_recorded_in_library_dirs_or_in_a_folder_holding_it(tmp_path, library_and_downloads) -> None:
    library, _downloads = library_and_downloads
    (library / "Artist").mkdir()

    conversions.record_conversion(str(library / "Artist" / "Album [WEB FLAC]"), **DOWNCONVERT)
    # Beside the library, in the folder that holds it.
    conversions.record_conversion(str(tmp_path / "Album [WEB FLAC]"), **DOWNCONVERT)

    assert _records_under(tmp_path) == []


def test_a_conversion_cannot_land_in_the_library_nor_leave_a_record_there(
    tmp_path, monkeypatch, library_and_downloads
) -> None:
    library, downloads = library_and_downloads
    src = downloads / "Album [WEB 24bit FLAC]"
    src.mkdir()
    _stub_convert(monkeypatch, library / "Album [WEB FLAC]", [SimpleNamespace(src="01.flac", target_rate=44100)])

    with pytest.raises(UploadError):
        anyio.run(dc.convert_folder, str(src), 16, 44100)

    _stub_transcode(monkeypatch, library / "Album [WEB MP3 V0]", [SimpleNamespace(src="01.flac")])
    with pytest.raises(UploadError):
        anyio.run(tc.transcode_folder, str(src), "V0")

    assert _records_under(tmp_path) == []


def test_a_conversion_into_download_directory_is_recorded_beside_it(monkeypatch, library_and_downloads) -> None:
    _library, downloads = library_and_downloads
    src = downloads / "Album [WEB 24bit FLAC]"
    src.mkdir()
    out = downloads / "Album [WEB FLAC]"
    _stub_convert(monkeypatch, out, [SimpleNamespace(src="01.flac", target_rate=44100)])

    anyio.run(dc.convert_folder, str(src), 16, 44100)

    assert [path.parent for path in _records_under(downloads)] == [downloads]


def test_a_dry_run_records_only_inside_its_scratch_directory(tmp_path, library_and_downloads) -> None:
    _library, downloads = library_and_downloads
    scratch = downloads / ".salmon-staging" / "run-1"
    scratch.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere" / "Album [WEB FLAC]"

    with dryrun.mode(), dryrun.writing_into(str(scratch)):
        conversions.record_conversion(str(elsewhere), **DOWNCONVERT)
        conversions.record_conversion(str(scratch / "Album [WEB FLAC]"), **DOWNCONVERT)

    assert [path.parent for path in _records_under(tmp_path)] == [scratch]


def test_a_dry_run_without_a_scratch_directory_records_nothing(tmp_path) -> None:
    with dryrun.mode():
        conversions.record_conversion(str(tmp_path / "Album [WEB FLAC]"), **DOWNCONVERT)

    assert _records_under(tmp_path) == []


def test_parallel_conversions_keep_each_others_records(tmp_path, monkeypatch) -> None:
    sources = [tmp_path / f"Album {n} [WEB FLAC]" for n in range(6)]
    for source in sources:
        source.mkdir()

    async def no_transcode(_items, _bitrate):
        await anyio.sleep(0)

    monkeypatch.setattr(tc, "_validate_lossless", lambda _path: None)
    monkeypatch.setattr(tc, "_build_output_path", lambda path, bitrate, *_a: f"{path} [{bitrate}]")
    monkeypatch.setattr(tc, "_collect_transcode_items", lambda *_a: [SimpleNamespace(src="01.flac")])
    monkeypatch.setattr(tc, "_copy_extra_files", lambda *_a, **_k: None)
    monkeypatch.setattr(tc, "_transcode_audio_files", no_transcode)

    async def run() -> None:
        async with anyio.create_task_group() as group:
            for source in sources:
                group.start_soon(tc.transcode_folder, str(source), "V0")

    anyio.run(run)

    recorded = {(_facts(f"{source} [V0]") or {}).get("source") for source in sources}
    assert recorded == {str(source) for source in sources}


# A separate `salmon up` of a converted folder


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path, Path]:
    """A library, a download_directory and a dot_torrents_dir, configured."""
    library, downloads, torrents = tmp_path / "library", tmp_path / "downloads", tmp_path / "torrents"
    for folder in (library, downloads, torrents):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    return library, downloads, torrents


def _flac_uploads(run) -> list[dict]:
    """The form of each upload of the run, in order: the FLAC and its two transcodes, per tracker."""
    return [dict(sent.fields) for sent in run.tracker.sent if sent.query.get("action") == "upload"]


# The MP3 transcodes the run makes always carry their own note: only the FLAC uploads tell.
NOTE = "[b]Transcode process:[/b]"


@pytest.mark.usefixtures("image_uploads")
def test_a_converted_folder_uploaded_on_its_own_describes_the_conversion(monkeypatch, tmp_path, dirs) -> None:
    _library, downloads, torrents = dirs
    album = _album(tmp_path / "seeding" / "Album [WEB FLAC]")
    _write_record(album, DOWNCONVERT)

    run = _run_up(monkeypatch, album, torrents)

    assert run.result.exit_code == 0, run.result.output
    uploads = _flac_uploads(run)
    assert len(uploads) == 6
    for upload in (uploads[0], uploads[3]):
        # The note is added to the normal description: its spectral links and its one footer stay.
        assert "[b]Transcode process:[/b]" in upload["release_desc"]
        assert "[img=https://images.test/" in upload["release_desc"]
        assert upload["release_desc"].count("Uploaded with") == 1
        assert "sox input.flac -R -G -b 16 output.flac rate -v -L 44100 dither" in upload["release_desc"]
    # The renamed folder carries the record along, and no torrent holds it.
    assert _facts(str(downloads / RENAMED)) == DOWNCONVERT
    for upload in uploads:
        lines = _torrent_lines(upload["file_input"][1])
        assert not any("salmon-conversions" in line or ".json" in line for line in lines), lines


@pytest.mark.usefixtures("image_uploads")
def test_a_transcode_uploaded_on_its_own_describes_the_transcode(monkeypatch, tmp_path, dirs) -> None:
    _library, _downloads, torrents = dirs
    album = _album(tmp_path / "seeding" / "Album [WEB FLAC]")
    _write_record(album, {"source": "/x/src", "kind": "transcode", "bitrate": "V0"})

    run = _run_up(monkeypatch, album, torrents)

    assert run.result.exit_code == 0, run.result.output
    description = _flac_uploads(run)[0]["release_desc"]
    assert "lame -S -V 0" in description and "[img=https://images.test/" in description
    assert description.count("Uploaded with") == 1


@pytest.mark.usefixtures("image_uploads")
def test_an_upload_without_a_record_has_no_conversion_note(monkeypatch, tmp_path, dirs) -> None:
    _library, _downloads, torrents = dirs
    album = _album(tmp_path / "seeding" / "Album [WEB FLAC]")

    run = _run_up(monkeypatch, album, torrents)

    assert run.result.exit_code == 0, run.result.output
    assert all(NOTE not in upload["release_desc"] for upload in _flac_uploads(run) if upload["format"] == "FLAC")


@pytest.mark.usefixtures("image_uploads")
@pytest.mark.parametrize("record", ["malformed", "foreign"])
def test_a_malformed_or_foreign_record_is_ignored_and_the_upload_goes_on(
    monkeypatch, tmp_path, dirs, record: str
) -> None:
    _library, _downloads, torrents = dirs
    album = _album(tmp_path / "seeding" / "Album [WEB FLAC]")
    if record == "malformed":
        _write_record(album, {"kind": "downconvert", "bit_depth": 12})
    else:
        other = tmp_path / "seeding" / "Other [WEB FLAC]"
        conversions.record_conversion(str(other), **DOWNCONVERT)
        os.replace(conversions._sidecar(str(other)), conversions._sidecar(str(album)))

    run = _run_up(monkeypatch, album, torrents)

    assert run.result.exit_code == 0, run.result.output
    assert "Ignoring a conversion record that does not fit" in run.result.output
    assert all(NOTE not in upload["release_desc"] for upload in _flac_uploads(run) if upload["format"] == "FLAC")


@pytest.mark.usefixtures("image_uploads")
def test_a_dry_run_describes_the_conversion_and_leaves_no_record_behind(monkeypatch, tmp_path, dirs) -> None:
    _library, downloads, torrents = dirs
    album = _album(tmp_path / "seeding" / "Album [WEB FLAC]")
    _write_record(album, DOWNCONVERT)

    run = _run_up(monkeypatch, album, torrents, args=("--dry-run",))

    assert run.result.exit_code == 0, run.result.output
    assert "[b]Transcode process:[/b]" in run.result.output
    assert _records_under(downloads) == []
    assert _facts(str(album)) == DOWNCONVERT


def test_a_failed_write_leaves_no_temporary_file_behind(tmp_path, monkeypatch) -> None:
    def full_disk(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(conversions.json, "dump", full_disk)

    with pytest.raises(OSError):
        conversions.record_conversion(str(tmp_path / "Album [WEB FLAC]"), **DOWNCONVERT)

    assert list((tmp_path / conversions.REGISTRY_DIR).iterdir()) == []
