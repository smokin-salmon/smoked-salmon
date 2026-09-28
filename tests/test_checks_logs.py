import os
import struct
from dataclasses import dataclass, field
from importlib import import_module
from types import SimpleNamespace

import anyio
import cambia
import pytest
from mutagen.flac import FLAC

from salmon import cfg
from salmon.errors import CRCMismatchError, EditedLogError, LogCheckSkipped
from salmon.tagger.retagger import rename_files

# salmon.checks.__init__ likely defines click commands that could shadow submodules on the
# package object, so importlib is used to get the module itself, matching test_checks_integrity.
logs = import_module("salmon.checks.logs")


@dataclass
class FakeTestAndCopy:
    copy_hash: str


@dataclass
class FakeTrack:
    num: int
    copy_hash: str
    is_range: bool = False

    @property
    def test_and_copy(self) -> FakeTestAndCopy:
        return FakeTestAndCopy(self.copy_hash)


@dataclass
class FakeTocHash:
    hash: str


@dataclass
class FakeTocRaw:
    entries: list = field(default_factory=list)


@dataclass
class FakeToc:
    accurip_tocid: FakeTocHash
    raw: FakeTocRaw = field(default_factory=FakeTocRaw)


@dataclass
class FakeChecksum:
    integrity: cambia.Integrity = field(default_factory=lambda: cambia.Integrity.Match)


@dataclass
class FakeParsedLog:
    tracks: list[FakeTrack]
    toc_hash: str = "disc-1"
    checksum: FakeChecksum = field(default_factory=FakeChecksum)
    toc_entries: list = field(default_factory=list)

    @property
    def toc(self) -> FakeToc:
        return FakeToc(accurip_tocid=FakeTocHash(hash=self.toc_hash), raw=FakeTocRaw(entries=self.toc_entries))


@dataclass
class FakeParsedCombined:
    parsed_logs: list[FakeParsedLog]


@dataclass
class FakeEvaluationCombined:
    combined_score: str = "100"


@dataclass
class FakeCambiaOutput:
    parsed: FakeParsedCombined
    evaluation_combined: list[FakeEvaluationCombined] = field(default_factory=lambda: [FakeEvaluationCombined()])


def _patch_cambia(monkeypatch, output: FakeCambiaOutput) -> None:
    monkeypatch.setattr(logs.cambia, "parse_log_file", lambda path: output)


def _patch_file_crcs(monkeypatch, crc_by_name: dict[str, str]) -> None:
    async def fake_calculate_file_crc_async(filepath: str, _: object = None) -> str:
        return crc_by_name[filepath.rsplit("/", 1)[-1]]

    monkeypatch.setattr(logs, "_calculate_file_crc_async", fake_calculate_file_crc_async)


def _write_files(tmp_path, names: list[str]) -> str:
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"audio")
    return str(tmp_path)


def _record_file_crcs(monkeypatch, crc_by_name: dict[str, str]) -> list[str]:
    checked: list[str] = []

    async def fake_calculate_file_crc_async(filepath: str, _: object = None) -> str:
        checked.append(filepath)
        return crc_by_name[filepath.rsplit("/", 1)[-1]]

    monkeypatch.setattr(logs, "_calculate_file_crc_async", fake_calculate_file_crc_async)
    return checked


def _one_disc_log(*copy_hashes: str, is_range: bool = False, toc_entries: list | None = None) -> FakeCambiaOutput:
    tracks = [FakeTrack(num=i, copy_hash=h, is_range=is_range) for i, h in enumerate(copy_hashes, 1)]
    parsed_log = FakeParsedLog(tracks=tracks, toc_entries=toc_entries or [])
    return FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=[parsed_log]))


def _two_disc_log(disc1: list[str], disc2: list[str]) -> FakeCambiaOutput:
    parsed_logs = [
        FakeParsedLog(toc_hash=toc_hash, tracks=[FakeTrack(num=i, copy_hash=h) for i, h in enumerate(hashes, 1)])
        for toc_hash, hashes in (("disc-1", disc1), ("disc-2", disc2))
    ]
    return FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=parsed_logs))


def _write_discs(tmp_path, discs: dict[str, list[str]]) -> str:
    for disc, names in discs.items():
        (tmp_path / disc).mkdir()
        for name in names:
            (tmp_path / disc / name).write_bytes(b"audio")
    return str(tmp_path)


def test_appended_rerip_replaces_the_stale_hash(tmp_path, monkeypatch) -> None:
    basepath = _write_files(tmp_path, ["01.flac", "02.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(tracks=[FakeTrack(num=1, copy_hash="STALE1"), FakeTrack(num=2, copy_hash="MATCH2")]),
                # Appended rerip log for track 1 only, with the current, correct hash.
                FakeParsedLog(tracks=[FakeTrack(num=1, copy_hash="MATCH1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"01.flac": "MATCH1", "02.flac": "MATCH2"})

    anyio.run(logs.check_log_cambia, "log.log", basepath)


def test_two_discs_with_overlapping_track_numbers_keep_both_hashes(tmp_path, monkeypatch) -> None:
    # Both discs use track number 1. If the expected hash were keyed by track number alone,
    # disc 2's entry would silently overwrite disc 1's, and a real corruption on disc 1 track 1
    # would go undetected because its expected hash was dropped from copy_crc_set. Keying by
    # (disc, track) keeps both expectations, so the corrupt disc-1 file must still be caught.
    basepath = _write_files(tmp_path, ["d1-01.flac", "d2-01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "CORRUPTED", "d2-01.flac": "D2-1"})

    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


def test_a_real_mismatch_still_raises(tmp_path, monkeypatch) -> None:
    basepath = _write_files(tmp_path, ["01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=[FakeTrack(num=1, copy_hash="EXPECTED")])])
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"01.flac": "DIFFERENT"})

    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


def test_multi_disc_range_rip_is_skipped_with_a_notice(tmp_path, monkeypatch) -> None:
    basepath = _write_files(tmp_path, ["range1.flac", "range2.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="R1", is_range=True)]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="R2", is_range=True)]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)

    async def fail_range_crc(*args: object, **kwargs: object) -> str:
        raise AssertionError("range CRC should not be computed for a skipped multi-disc range rip")

    monkeypatch.setattr(logs, "_calculate_range_crc_async", fail_range_crc)

    with pytest.raises(LogCheckSkipped, match="Multi-disc range rip"):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


TWO_DISC_CRCS = {"d1-01.flac": "D1-1", "d1-02.flac": "D1-2", "d2-01.flac": "D2-1", "d2-02.flac": "D2-2"}


def test_a_log_in_a_disc_folder_checks_only_that_disc(tmp_path, monkeypatch) -> None:
    # One log per disc folder: each log's CRCs are checked against its own disc's files, not
    # against every file in the release, which decoded a 3-disc release three times (#444).
    basepath = _write_files(
        tmp_path, ["CD1/CD1.log", "CD1/d1-01.flac", "CD1/d1-02.flac", "CD2/CD2.log", "CD2/d2-01.flac", "CD2/d2-02.flac"]
    )
    _patch_cambia(monkeypatch, _one_disc_log("D1-1", "D1-2"))
    checked = _record_file_crcs(monkeypatch, TWO_DISC_CRCS)

    anyio.run(logs.check_log_cambia, str(tmp_path / "CD1" / "CD1.log"), basepath)

    assert sorted(checked) == [str(tmp_path / "CD1" / "d1-01.flac"), str(tmp_path / "CD1" / "d1-02.flac")]


def test_a_range_rip_log_in_a_disc_folder_rebuilds_only_that_disc(tmp_path, monkeypatch) -> None:
    basepath = _write_files(
        tmp_path, ["CD1/CD1.log", "CD1/d1-01.flac", "CD1/d1-02.flac", "CD2/CD2.log", "CD2/d2-01.flac", "CD2/d2-02.flac"]
    )
    _patch_cambia(monkeypatch, _one_disc_log("RANGE2", is_range=True, toc_entries=["track 1", "track 2"]))
    rebuilt_from: list[str] = []

    async def fake_range_crc(track_files: list[str], toc_entries: list) -> str:
        rebuilt_from.extend(track_files)
        return "RANGE2"

    monkeypatch.setattr(logs, "_calculate_range_crc_async", fake_range_crc)

    anyio.run(logs.check_log_cambia, str(tmp_path / "CD2" / "CD2.log"), basepath)

    assert sorted(rebuilt_from) == [str(tmp_path / "CD2" / "d2-01.flac"), str(tmp_path / "CD2" / "d2-02.flac")]


@pytest.mark.parametrize(
    "audio",
    [
        pytest.param(["d1-01.flac", "d1-02.flac"], id="audio-in-root"),
        pytest.param(["CD1/d1-01.flac", "CD1/d1-02.flac", "CD2/d2-01.flac", "CD2/d2-02.flac"], id="audio-in-discs"),
    ],
)
def test_a_log_in_a_folder_without_audio_checks_the_whole_release(tmp_path, monkeypatch, audio) -> None:
    basepath = _write_files(tmp_path, ["Logs/CD1.log", *audio])
    _patch_cambia(monkeypatch, _one_disc_log("D1-1", "D1-2"))
    checked = _record_file_crcs(monkeypatch, TWO_DISC_CRCS)

    anyio.run(logs.check_log_cambia, str(tmp_path / "Logs" / "CD1.log"), basepath)

    assert sorted(checked) == sorted(str(tmp_path / name) for name in audio)


def test_a_single_folder_release_checks_its_files(tmp_path, monkeypatch) -> None:
    basepath = _write_files(tmp_path, ["album.log", "d1-01.flac", "d1-02.flac"])
    _patch_cambia(monkeypatch, _one_disc_log("D1-1", "D1-2"))
    checked = _record_file_crcs(monkeypatch, TWO_DISC_CRCS)

    anyio.run(logs.check_log_cambia, str(tmp_path / "album.log"), basepath)

    assert sorted(checked) == [str(tmp_path / "d1-01.flac"), str(tmp_path / "d1-02.flac")]


def _write_flac(path, discnumber: str | None) -> None:
    """Write a FLAC file with no audio: a STREAMINFO block, and a DISCNUMBER tag if one is given."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    if discnumber is not None:
        tagged = FLAC(path)
        tagged["discnumber"] = discnumber
        tagged.save()


def _one_folder_release(tmp_path, monkeypatch) -> list[str]:
    """Rename a two-disc release with a log in each disc folder into one folder, and return its logs."""
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    tags = {}
    for disc in (1, 2):
        for track in (1, 2):
            name = f"CD{disc}/d{disc}-0{track}.flac"
            _write_flac(tmp_path / name, f"{disc}/2")
            tags[name] = SimpleNamespace(artist=["A"], title="T", tracknumber=str(track), discnumber=str(disc))
        (tmp_path / f"CD{disc}" / "rip.log").write_text(f"disc {disc}")
    metadata = {"tracks": {d: {t: {"artists": [("A", "main")]} for t in ("1", "2")} for d in ("1", "2")}}

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert sorted(os.listdir(tmp_path)) == [*ONE_FOLDER_CRCS, "rip.1.log", "rip.2.log"]
    return [str(tmp_path / "rip.1.log"), str(tmp_path / "rip.2.log")]


def _patch_cambia_per_log(monkeypatch, output_by_log: dict[str, FakeCambiaOutput]) -> None:
    monkeypatch.setattr(logs.cambia, "parse_log_file", lambda path: output_by_log[os.path.basename(path)])


ONE_FOLDER_CRCS = {"1.01.flac": "D1-1", "1.02.flac": "D1-2", "2.01.flac": "D2-1", "2.02.flac": "D2-2"}
ONE_FOLDER_LOGS = {"rip.1.log": _one_disc_log("D1-1", "D1-2"), "rip.2.log": _one_disc_log("D2-1", "D2-2")}


def test_a_log_named_for_its_disc_checks_only_that_disc_in_a_one_folder_release(tmp_path, monkeypatch) -> None:
    log_paths = _one_folder_release(tmp_path, monkeypatch)
    _patch_cambia_per_log(monkeypatch, ONE_FOLDER_LOGS)
    checked = _record_file_crcs(monkeypatch, ONE_FOLDER_CRCS)

    # As the upload checks them: each log found, against the release folder
    checked_by_log = {}
    for log_path in log_paths:
        anyio.run(logs.check_log_cambia, log_path, str(tmp_path))
        checked_by_log[os.path.basename(log_path)] = sorted(os.path.basename(path) for path in checked)
        checked.clear()

    assert checked_by_log == {"rip.1.log": ["1.01.flac", "1.02.flac"], "rip.2.log": ["2.01.flac", "2.02.flac"]}


@pytest.mark.parametrize(("bad_log", "bad_track"), [("rip.1.log", "1.02.flac"), ("rip.2.log", "2.01.flac")])
def test_a_bad_track_fails_its_discs_log_in_a_one_folder_release(tmp_path, monkeypatch, bad_log, bad_track) -> None:
    log_paths = _one_folder_release(tmp_path, monkeypatch)
    _patch_cambia_per_log(monkeypatch, ONE_FOLDER_LOGS)
    _patch_file_crcs(monkeypatch, {**ONE_FOLDER_CRCS, bad_track: "CORRUPTED"})

    for log_path in log_paths:
        if os.path.basename(log_path) == bad_log:
            with pytest.raises(CRCMismatchError):
                anyio.run(logs.check_log_cambia, log_path, str(tmp_path))
        else:
            anyio.run(logs.check_log_cambia, log_path, str(tmp_path))


@pytest.mark.parametrize(("range_crc", "matches"), [("RANGE2", True), ("OTHER", False)])
def test_a_range_rip_log_named_for_its_disc_rebuilds_only_that_disc(tmp_path, monkeypatch, range_crc, matches) -> None:
    log_paths = _one_folder_release(tmp_path, monkeypatch)
    range_log = _one_disc_log("RANGE2", is_range=True, toc_entries=["track 1", "track 2"])
    _patch_cambia_per_log(monkeypatch, {"rip.2.log": range_log})
    rebuilt_from: list[str] = []

    async def fake_range_crc(track_files: list[str], toc_entries: list) -> str:
        rebuilt_from.extend(track_files)
        return range_crc

    monkeypatch.setattr(logs, "_calculate_range_crc_async", fake_range_crc)

    if matches:
        anyio.run(logs.check_log_cambia, log_paths[1], str(tmp_path))
    else:
        with pytest.raises(CRCMismatchError):
            anyio.run(logs.check_log_cambia, log_paths[1], str(tmp_path))

    assert sorted(os.path.basename(path) for path in rebuilt_from) == ["2.01.flac", "2.02.flac"]


@pytest.mark.parametrize(
    ("log_name", "discs"),
    [
        pytest.param("rip.log", ["1", "1", "2", "2"], id="log-not-named-for-a-disc"),
        pytest.param("rip.1.log", ["1", "1", "2", None], id="a-track-without-disc-number"),
        pytest.param("rip.1.log", ["1", "1", "1", "1"], id="all-one-disc"),
        pytest.param("rip.3.log", ["1", "1", "2", "2"], id="no-track-of-that-disc"),
    ],
)
def test_a_log_is_checked_against_every_track_when_its_disc_is_unclear(tmp_path, monkeypatch, log_name, discs) -> None:
    for name, disc in zip(ONE_FOLDER_CRCS, discs, strict=True):
        _write_flac(tmp_path / name, disc)
    (tmp_path / log_name).write_text("log")
    _patch_cambia(monkeypatch, _one_disc_log("D1-1", "D1-2"))
    checked = _record_file_crcs(monkeypatch, ONE_FOLDER_CRCS)

    anyio.run(logs.check_log_cambia, str(tmp_path / log_name), str(tmp_path))

    assert sorted(os.path.basename(path) for path in checked) == list(ONE_FOLDER_CRCS)


def test_an_unparseable_log_is_skipped(tmp_path, monkeypatch) -> None:
    def parse_log_file(_path):
        raise ValueError("not a log")

    monkeypatch.setattr(logs.cambia, "parse_log_file", parse_log_file)
    with pytest.raises(LogCheckSkipped, match="not a log"):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), str(tmp_path))


def test_an_unreadable_log_file_is_an_error_not_a_skip(tmp_path, monkeypatch) -> None:
    def parse_log_file(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(logs.cambia, "parse_log_file", parse_log_file)
    with pytest.raises(PermissionError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), str(tmp_path))


def test_a_log_without_a_score_is_still_checked(tmp_path, monkeypatch, capsys) -> None:
    disc = _write_files(tmp_path, ["d1-01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=[FakeTrack(1, "D1-1")])]),
        evaluation_combined=[],
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "OTHER"})
    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), disc)
    out = capsys.readouterr().out
    assert "Could not read the log score" in out


def test_a_log_with_no_audio_is_skipped(tmp_path, monkeypatch) -> None:
    output = FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=[FakeTrack(1, "D1-1")])]))
    _patch_cambia(monkeypatch, output)
    with pytest.raises(LogCheckSkipped, match="No audio files found"):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), str(tmp_path))


def test_a_log_with_no_tracks_is_skipped_not_passed(tmp_path, monkeypatch) -> None:
    disc = _write_files(tmp_path, ["d1-01.flac"])
    _patch_cambia(monkeypatch, FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=[])])))
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "CORRUPTED"})
    with pytest.raises(LogCheckSkipped, match="no tracks"):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), disc)


def test_appended_logs_without_a_toc_are_skipped_not_merged(tmp_path, monkeypatch) -> None:
    disc = _write_files(tmp_path, ["d1-01.flac", "d2-01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "CORRUPTED", "d2-01.flac": "D2-1"})
    with pytest.raises(LogCheckSkipped, match="without a TOC"):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), disc)


def test_a_range_rip_with_more_files_than_its_toc_is_skipped_not_aborted(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac", "d1-02.flac"], "CD2": ["d2-01.flac", "d2-02.flac"]})
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(
                    tracks=[FakeTrack(num=1, copy_hash="R1", is_range=True)], toc_entries=["track 1", "track 2"]
                )
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    # The log sits at the album root, so its folder search finds both discs' audio.
    with pytest.raises(LogCheckSkipped, match="Range rip of 2 tracks, but 4 audio file"):
        anyio.run(logs.check_log_cambia, str(tmp_path / "CD1.log"), basepath)


def _fail_scandir(monkeypatch, folder: str, error: OSError) -> None:
    real_scandir = os.scandir

    def scandir(path):
        if os.path.basename(path) == folder:
            raise error
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)


def test_a_multi_disc_track_rip_log_in_a_disc_folder_checks_every_disc(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac"], "CD2": ["d2-01.flac"]})
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    logpath = str(tmp_path / "CD1" / "rip.log")

    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})
    anyio.run(logs.check_log_cambia, logpath, basepath)

    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "CORRUPTED"})
    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, logpath, basepath)


def test_a_one_disc_range_rip_is_rebuilt_from_its_own_disc_folder(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac", "d1-02.flac"], "CD2": ["d2-01.flac"]})
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(
                    toc_hash="disc-1",
                    tracks=[FakeTrack(num=1, copy_hash="R1", is_range=True)],
                    toc_entries=["track 1", "track 2"],
                )
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    rebuilt_from: list[list[str]] = []

    async def fake_range_crc(track_files: list[str], _toc_entries: list) -> str:
        rebuilt_from.append(sorted(os.path.basename(f) for f in track_files))
        return "R1"

    monkeypatch.setattr(logs, "_calculate_range_crc_async", fake_range_crc)

    anyio.run(logs.check_log_cambia, str(tmp_path / "CD1" / "rip.log"), basepath)

    assert rebuilt_from == [["d1-01.flac", "d1-02.flac"]]


def test_a_multi_disc_log_missing_other_discs_audio_is_skipped_not_failed(tmp_path, monkeypatch) -> None:
    # Only one disc's audio under the search root: a skip, not a CRC mismatch.
    disc = _write_files(tmp_path, ["d1-01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1"})

    with pytest.raises(LogCheckSkipped, match="only 1 audio file"):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), disc)


def test_an_unreadable_disc_folder_is_an_error_not_a_skip(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac"], "CD2": ["d2-01.flac"]})
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})
    _fail_scandir(monkeypatch, "CD2", PermissionError(13, "Permission denied"))
    with pytest.raises(PermissionError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "CD1" / "rip.log"), basepath)


def test_an_unreadable_search_root_is_an_error_not_no_audio(tmp_path, monkeypatch) -> None:
    disc = _write_files(tmp_path, ["d1-01.flac"])
    output = FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=[FakeTrack(1, "D1-1")])]))
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1"})
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if os.fspath(path) == disc:
            raise PermissionError(13, "Permission denied", path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    with pytest.raises(PermissionError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), disc)


def test_a_disc_folder_that_vanishes_mid_scan_is_an_error(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac"], "CD2": ["d2-01.flac"]})
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})
    _fail_scandir(monkeypatch, "CD2", FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(FileNotFoundError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "CD1" / "rip.log"), basepath)


def test_checklog_on_a_folder_checks_each_log_against_that_folder(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac"], "CD2": ["d2-01.flac"]})
    (tmp_path / "CD1" / "rip.log").write_text("log")
    seen: list[tuple[str, str]] = []

    async def fake_check(logpath: str, base: str) -> None:
        seen.append((logpath, base))

    checks = import_module("salmon.checks")
    monkeypatch.setattr(checks, "check_log_cambia", fake_check)

    anyio.run(checks.log.callback, basepath)

    assert seen == [(str(tmp_path / "CD1" / "rip.log"), basepath)]


def test_a_range_rip_on_a_later_disc_skips_the_combined_check(tmp_path, monkeypatch) -> None:
    basepath = _write_files(tmp_path, ["d1-01.flac", "d2-range.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="R2", is_range=True)]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    # Disc 2's range CRC matches no single file, so a per-file check would call a good rip a mismatch.
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-range.flac": "FILE-CRC"})

    with pytest.raises(LogCheckSkipped, match="Multi-disc range rip"):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


def test_a_range_entry_replaced_by_a_rerip_is_verified(tmp_path, monkeypatch, capsys) -> None:
    basepath = _write_files(tmp_path, ["d1-01.flac", "d2-01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="R2", is_range=True)]),
                # Appended rerip of disc 2 as a track rip: the latest entry is what's checked.
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})

    anyio.run(logs.check_log_cambia, "log.log", basepath)

    out = capsys.readouterr().out
    assert "All CRC values match" in out


@pytest.mark.parametrize("multi_disc", [True, False], ids=["multi-disc", "one-disc"])
def test_a_first_log_range_rip_replaced_by_a_rerip_is_verified_per_file(
    tmp_path, monkeypatch, capsys, multi_disc
) -> None:
    names = ["d1-01.flac", "d2-01.flac"] if multi_disc else ["d1-01.flac"]
    basepath = _write_files(tmp_path, names)
    logs_ = [
        FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="R1", is_range=True)]),
        FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
    ]
    if multi_disc:
        logs_.append(FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]))
    _patch_cambia(monkeypatch, FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=logs_)))
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})

    async def no_range(*_args, **_kwargs) -> str:
        raise AssertionError("the range was replaced; nothing should be rebuilt from its TOC")

    monkeypatch.setattr(logs, "_calculate_range_crc_async", no_range)

    anyio.run(logs.check_log_cambia, "log.log", basepath)

    out = capsys.readouterr().out
    assert "All CRC values match" in out


@pytest.mark.parametrize("multi_disc", [True, False], ids=["multi-disc", "one-disc"])
def test_two_tracks_sharing_a_crc_each_need_a_matching_file(tmp_path, monkeypatch, multi_disc) -> None:
    # The same track on two discs, or two silent tracks: one good copy must not cover a corrupt one.
    basepath = _write_files(tmp_path, ["a.flac", "b.flac"])
    second_disc = "disc-2" if multi_disc else "disc-1"
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="SAME")]),
                FakeParsedLog(toc_hash=second_disc, tracks=[FakeTrack(num=2, copy_hash="SAME")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"a.flac": "SAME", "b.flac": "CORRUPTED"})

    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


@pytest.mark.parametrize("multi_disc", [True, False], ids=["multi-disc", "one-disc"])
def test_an_edited_appended_log_is_refused(tmp_path, monkeypatch, multi_disc) -> None:
    basepath = _write_files(tmp_path, ["a.flac", "b.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="A")]),
                FakeParsedLog(
                    toc_hash="disc-2" if multi_disc else "disc-1",
                    tracks=[FakeTrack(num=2, copy_hash="B")],
                    checksum=FakeChecksum(integrity=cambia.Integrity.Mismatch),
                ),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"a.flac": "A", "b.flac": "B"})

    with pytest.raises(EditedLogError):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


def test_an_appended_log_without_a_checksum_warns(tmp_path, monkeypatch, capsys) -> None:
    basepath = _write_files(tmp_path, ["a.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(tracks=[FakeTrack(num=1, copy_hash="STALE")]),
                FakeParsedLog(
                    tracks=[FakeTrack(num=1, copy_hash="A")],
                    checksum=FakeChecksum(integrity=cambia.Integrity.Unknown),
                ),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"a.flac": "A"})

    anyio.run(logs.check_log_cambia, "log.log", basepath)

    out = capsys.readouterr().out
    assert "Lacking a valid checksum" in out


@pytest.mark.parametrize("log_name", ["rip.log", "rip.1.log"])
@pytest.mark.parametrize(("bad_track", "matches"), [(None, True), ("2.01.flac", False)], ids=["good", "bad-disc-2"])
def test_a_multi_disc_log_checks_every_disc_in_a_one_folder_release(
    tmp_path, monkeypatch, log_name, bad_track, matches
) -> None:
    # One log covering both discs, kept in a one-folder release (split_multi_disc_into_folders = false):
    # it is checked against both discs' tracks, even when named for one disc, since it holds both discs' CRCs.
    for name, disc in zip(ONE_FOLDER_CRCS, ["1", "1", "2", "2"], strict=True):
        _write_flac(tmp_path / name, f"{disc}/2")
    (tmp_path / log_name).write_text("log")
    _patch_cambia(monkeypatch, _two_disc_log(["D1-1", "D1-2"], ["D2-1", "D2-2"]))
    checked = _record_file_crcs(monkeypatch, {**ONE_FOLDER_CRCS, **({bad_track: "CORRUPTED"} if bad_track else {})})

    if matches:
        anyio.run(logs.check_log_cambia, str(tmp_path / log_name), str(tmp_path))
    else:
        with pytest.raises(CRCMismatchError):
            anyio.run(logs.check_log_cambia, str(tmp_path / log_name), str(tmp_path))

    assert sorted(os.path.basename(path) for path in checked) == list(ONE_FOLDER_CRCS)


def test_a_multi_disc_log_named_for_its_first_disc_passes_after_the_one_folder_rename(tmp_path, monkeypatch) -> None:
    # A two-disc log kept in CD1/ becomes rip.1.log once the discs are merged into one folder.
    log_paths = _one_folder_release(tmp_path, monkeypatch)
    _patch_cambia_per_log(
        monkeypatch,
        {"rip.1.log": _two_disc_log(["D1-1", "D1-2"], ["D2-1", "D2-2"]), "rip.2.log": ONE_FOLDER_LOGS["rip.2.log"]},
    )
    _patch_file_crcs(monkeypatch, ONE_FOLDER_CRCS)

    for log_path in log_paths:
        anyio.run(logs.check_log_cambia, log_path, str(tmp_path))
