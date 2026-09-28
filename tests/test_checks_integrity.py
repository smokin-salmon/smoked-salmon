import subprocess
from importlib import import_module

import anyio
import asyncclick as click
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
    assert result.md5_unset == ("track01.flac",)
    assert "no MD5 signature stored" in integrity.format_integrity(result)
    assert "\u2014" not in integrity.format_integrity(result)


def test_clean_flac_output_still_passes(monkeypatch) -> None:
    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 0, stdout=b"track01.flac: testing... ok\n", stderr=b"")

    monkeypatch.setattr(integrity.anyio, "run_process", fake_run_process)

    result = anyio.run(integrity._check_flac_integrity, "track01.flac")

    assert result.passed is True
    assert "MD5" not in result.details


# flac -wt's real output (on stderr) for an album of files with no MD5 in STREAMINFO. flac 1.3 draws its
# progress with backspaces even when its output is a pipe; 1.4 and later print no progress there.
FLAC_13_MD5_UNSET = (
    b"%s: testing, 26%% complete"
    + b"\x08" * 21
    + b"testing, 53%% complete"
    + b"\x08" * 21
    + b"testing, 79%% complete"
    + b"\x08" * 21
    + b"WARNING, cannot check MD5 signature since it was unset in the STREAMINFO\nok                    \n"
)
FLAC_15_MD5_UNSET = (
    b"%s: WARNING, cannot check MD5 signature since it was unset in the STREAMINFO\nok                    \n"
)
FLAC_15_CLEAN = b"%s: ok                    \n"
FLAC_15_TRUNCATED = (
    b"%s: *** Got error code 0:FLAC__STREAM_DECODER_ERROR_STATUS_LOST_SYNC after processing 77824 samples\n\n\n"
    b"%s: ERROR during decoding\n        state = FLAC__STREAM_DECODER_END_OF_STREAM\n"
)


def _fake_flac(monkeypatch, outputs: dict[str, bytes]) -> None:
    """Answer `flac -wt <file>` with the output given for the file's name; exit 1 unless it is a clean one."""

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert commands[:2] == ["flac", "-wt"]
        name = commands[2].rsplit("/", 1)[-1].encode()
        output = outputs[name.decode()]
        stderr = output % ((name,) * output.count(b"%s"))
        return subprocess.CompletedProcess(commands, 0 if output is FLAC_15_CLEAN else 1, stdout=b"", stderr=stderr)

    monkeypatch.setattr(integrity.anyio, "run_process", fake_run_process)


def _album(tmp_path, names) -> str:
    for name in names:
        (tmp_path / name).write_bytes(b"fLaC")
    return str(tmp_path)


@pytest.mark.parametrize("output", [FLAC_13_MD5_UNSET, FLAC_15_MD5_UNSET], ids=["flac-1.3", "flac-1.5"])
def test_an_album_with_no_md5_is_reported_once_per_file_without_backspaces(
    monkeypatch, tmp_path, output: bytes
) -> None:
    names = [f"{n:02d}.flac" for n in range(1, 13)]
    _fake_flac(monkeypatch, dict.fromkeys(names, output))

    result = anyio.run(integrity.check_integrity, _album(tmp_path, names))
    rendered = click.unstyle(integrity.format_integrity(result))

    assert "\x08" not in rendered
    assert "% complete" not in rendered
    assert sorted(result.md5_unset) == names
    assert result.decode_failures == ()
    assert result.checked == 12
    # Once for the album, not once or twice per file.
    assert result.details == ""
    assert ".flac" not in rendered
    assert "12 of 12 file(s) have no MD5 signature stored" in rendered
    assert rendered.splitlines()[0].endswith("Integrity check not passed: no MD5 signature stored")


def test_a_few_files_with_no_md5_are_named(monkeypatch, tmp_path) -> None:
    _fake_flac(monkeypatch, {"01.flac": FLAC_15_MD5_UNSET, "02.flac": FLAC_15_CLEAN, "03.flac": FLAC_15_CLEAN})

    result = anyio.run(integrity.check_integrity, _album(tmp_path, ["01.flac", "02.flac", "03.flac"]))

    assert "1 of 3 file(s) have no MD5 signature stored: 01.flac" in integrity.format_integrity(result)
    assert "1 of 3 file(s)" in integrity.sanitize_prompt(result)


def test_a_file_that_does_not_decode_is_a_decode_failure_even_beside_md5_unset_files(monkeypatch, tmp_path) -> None:
    _fake_flac(monkeypatch, {"01.flac": FLAC_15_MD5_UNSET, "02.flac": FLAC_15_TRUNCATED})

    result = anyio.run(integrity.check_integrity, _album(tmp_path, ["01.flac", "02.flac"]))
    rendered = click.unstyle(integrity.format_integrity(result))

    assert result.md5_unset == ("01.flac",)
    assert result.decode_failures == ("02.flac",)
    assert rendered.splitlines()[0].endswith("Failed integrity check")
    assert "02.flac: ERROR during decoding" in rendered
    assert integrity.sanitize_prompt(result) == "Do you want to sanitize this upload?"


def test_resolve_overstrikes() -> None:
    assert integrity._resolve_overstrikes("abc\x08\x08d") == "ad"
    assert integrity._resolve_overstrikes("a\nb\x08\x08c") == "a\nc"
    assert integrity._resolve_overstrikes("50%\rok") == "ok"


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
