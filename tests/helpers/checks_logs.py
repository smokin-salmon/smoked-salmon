"""Helpers of tests/test_checks_logs.py that other test modules use too."""

from dataclasses import dataclass, field
from importlib import import_module

import cambia

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


def _one_disc_log(*copy_hashes: str, is_range: bool = False, toc_entries: list | None = None) -> FakeCambiaOutput:
    tracks = [FakeTrack(num=i, copy_hash=h, is_range=is_range) for i, h in enumerate(copy_hashes, 1)]
    parsed_log = FakeParsedLog(tracks=tracks, toc_entries=toc_entries or [])
    return FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=[parsed_log]))
