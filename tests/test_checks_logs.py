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
from salmon.errors import CRCMismatchError
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

    @property
    def toc(self) -> FakeToc:
        return FakeToc(accurip_tocid=FakeTocHash(hash=self.toc_hash))


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


def _one_disc_log(*copy_hashes: str, is_range: bool = False) -> FakeCambiaOutput:
    tracks = [FakeTrack(num=i, copy_hash=h, is_range=is_range) for i, h in enumerate(copy_hashes, 1)]
    return FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=tracks)]))


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


def test_multi_disc_range_rip_is_skipped_with_a_notice(tmp_path, monkeypatch, capsys) -> None:
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

    anyio.run(logs.check_log_cambia, "log.log", basepath)

    assert "Multi-disc range rip" in capsys.readouterr().out


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
    _patch_cambia(monkeypatch, _one_disc_log("RANGE2", is_range=True))
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
    _patch_cambia_per_log(monkeypatch, {"rip.2.log": _one_disc_log("RANGE2", is_range=True)})
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
