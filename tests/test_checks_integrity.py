import subprocess
from importlib import import_module

import anyio
import pytest

from salmon.checks.integrity import IntegrityResult

# salmon.checks.__init__ defines a click command also named `integrity`, which shadows the
# submodule on the package object, so importlib is used to get the module itself.
integrity = import_module("salmon.checks.integrity")

# The real warning flac -wt prints when a file's STREAMINFO MD5 is unset (from #353). flac still
# exits 0 in this case, so the check must fail on the warning text, not on the return code alone.
MD5_UNSET_STDERR = b"track01.flac: WARNING, MD5 signature unset in STREAMINFO\ntrack01.flac: testing... ok\n"


def test_unset_streaminfo_md5_fails_the_check(monkeypatch) -> None:
    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 0, stdout=b"", stderr=MD5_UNSET_STDERR)

    monkeypatch.setattr(integrity.anyio, "run_process", fake_run_process)

    result = anyio.run(integrity._check_flac_integrity, "track01.flac")

    assert result.passed is False
    assert "MD5 signature unset in STREAMINFO" in result.details
    assert "\u2014" not in result.details


def test_clean_flac_output_still_passes(monkeypatch) -> None:
    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 0, stdout=b"track01.flac: testing... ok\n", stderr=b"")

    monkeypatch.setattr(integrity.anyio, "run_process", fake_run_process)

    result = anyio.run(integrity._check_flac_integrity, "track01.flac")

    assert result.passed is True
    assert "MD5" not in result.details


# mp3val 0.1.8's real output for files damaged in different ways (the full path it prints replaced by
# /music/a.mp3). It exits 0 for all of them.
MP3VAL_CLEAN = b"""Analyzing file "/music/a.mp3"...
INFO: "/music/a.mp3": 194 MPEG frames (MPEG 1 Layer III), +ID3v2, Xing header
Done!
"""
MP3VAL_TRUNCATED = b"""Analyzing file "/music/a.mp3"...
WARNING: "/music/a.mp3" (offset 0x7689): It seems that file is truncated or there is garbage at the end of the file
WARNING: "/music/a.mp3": Wrong number of MPEG frames specified in Xing header (193 instead of 73)
WARNING: "/music/a.mp3": Wrong number of MPEG data bytes specified in Xing header (80874 instead of 30301)
INFO: "/music/a.mp3": 73 MPEG frames (MPEG 1 Layer III), +ID3v2, Xing header
Done!
"""
MP3VAL_RESYNCHRONIZED = b"""Analyzing file "/music/a.mp3"...
WARNING: "/music/a.mp3" (offset 0x4f5a): MPEG stream error, resynchronized successfully
INFO: "/music/a.mp3": 194 MPEG frames (MPEG 1 Layer III), +ID3v2, Xing header
Done!
"""
MP3VAL_GARBAGE_AT_START = b"""Analyzing file "/music/a.mp3"...
WARNING: "/music/a.mp3" (offset 0x0): Garbage at the beginning of the file
WARNING: "/music/a.mp3": No supported tags in the file
INFO: "/music/a.mp3": 194 MPEG frames (MPEG 1 Layer III), no tags, Xing header
Done!
"""
MP3VAL_NOT_MPEG = b"""Analyzing file "/music/a.mp3"...
ERROR: "/music/a.mp3": Unknown file format
Done!
"""
MP3VAL_EMPTY = b"""Analyzing file "/music/a.mp3"...
WARNING: "/music/a.mp3": Too few MPEG frames (it's unlikely that this is a MPEG audio file)
WARNING: "/music/a.mp3": No supported tags in the file
INFO: "/music/a.mp3": No MPEG frames, no tags, CBR
Done!
"""
# The one message here not produced from a real file: mp3val prints it for a VBR file with no Xing/VBRI
# header, which only affects seeking.
MP3VAL_NO_VBR_HEADER = b"""Analyzing file "/music/a.mp3"...
WARNING: "/music/a.mp3": VBR detected, but no VBR header is present. Seeking may not work properly.
INFO: "/music/a.mp3": 7320 MPEG frames (MPEG 1 Layer III), +ID3v2, no VBR header
Done!
"""


def _mp3val(monkeypatch, stdout: bytes, stderr: bytes = b"") -> IntegrityResult:
    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert commands == ["mp3val", "/music/a.mp3"]
        return subprocess.CompletedProcess(commands, 0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(integrity.anyio, "run_process", fake_run_process)
    return anyio.run(integrity._check_mp3_integrity, "/music/a.mp3")


def test_clean_mp3_passes_with_nothing_to_report(monkeypatch) -> None:
    result = _mp3val(monkeypatch, MP3VAL_CLEAN)

    assert result.passed is True
    assert result.details == ""
    assert result.concerns == ()
    assert result.decode_failures == ()


@pytest.mark.parametrize(
    ("stdout", "reported"),
    [
        (MP3VAL_TRUNCATED, "a.mp3 (offset 0x7689): It seems that file is truncated"),
        (MP3VAL_RESYNCHRONIZED, "a.mp3 (offset 0x4f5a): MPEG stream error, resynchronized successfully"),
        (MP3VAL_GARBAGE_AT_START, "a.mp3 (offset 0x0): Garbage at the beginning of the file"),
    ],
)
def test_mp3_stream_damage_fails_the_check_but_can_be_acknowledged(monkeypatch, stdout, reported) -> None:
    """mp3val exits 0 while describing the damage; the check used to pass every mp3val run."""
    result = _mp3val(monkeypatch, stdout)

    assert result.passed is False
    assert reported in result.details
    assert "/music/" not in result.details
    assert result.decode_failures == (), "damage mp3val can repair is not a reason to refuse the upload"


def test_mp3_tag_notes_pass_as_concerns(monkeypatch) -> None:
    garbage = _mp3val(monkeypatch, MP3VAL_GARBAGE_AT_START)
    assert garbage.concerns == ("a.mp3: No supported tags in the file",)
    assert "No supported tags" not in garbage.details

    no_vbr_header = _mp3val(monkeypatch, MP3VAL_NO_VBR_HEADER)
    assert no_vbr_header.passed is True
    assert no_vbr_header.concerns[0].startswith("a.mp3: VBR detected, but no VBR header is present")


@pytest.mark.parametrize(
    ("stdout", "stderr", "reported"),
    [
        (MP3VAL_NOT_MPEG, b"", "Unknown file format"),
        (MP3VAL_EMPTY, b"", "Too few MPEG frames"),
        # A missing or unreadable file: mp3val writes only this, to stderr, and still exits 0.
        (b"", b'Cannot open input file "/music/a.mp3" or it is empty\n', "Cannot open input file"),
    ],
)
def test_a_file_that_is_not_mpeg_audio_is_a_decode_failure(monkeypatch, stdout, stderr, reported) -> None:
    result = _mp3val(monkeypatch, stdout, stderr)

    assert result.passed is False
    assert result.decode_failures == ("a.mp3",)
    assert reported in result.details
