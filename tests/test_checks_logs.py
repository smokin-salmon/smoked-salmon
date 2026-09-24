from dataclasses import dataclass, field
from importlib import import_module

import anyio
import cambia
import pytest

from salmon.errors import CRCMismatchError

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
