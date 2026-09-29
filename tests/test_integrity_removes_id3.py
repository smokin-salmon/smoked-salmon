"""Sanitizing re-encodes a FLAC from scratch, so it is expected to remove any ID3 tag the
original file carried. This pins that claim: the re-encode writes a fresh file with none of the
source bytes, which is exactly what makes it safe to say sanitizing removes the tag."""

import subprocess
from importlib import import_module

import anyio
import pytest

from salmon.checks.tag_rules import has_id3_tag

integrity = import_module("salmon.checks.integrity")


@pytest.fixture
def fake_flac_reencode(monkeypatch):
    """Mimic a real flac re-encode: the output holds none of the input's bytes, ID3 included."""

    async def run_process(commands: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if commands[0] == "metaflac":
            return subprocess.CompletedProcess(commands, 0, b"", b"")
        # flac -<level> <file>.corrupted -o <file>
        output_path = commands[4]
        with open(output_path, "wb") as f:
            f.write(b"fLaC" + b"\x00" * 300)
        return subprocess.CompletedProcess(commands, 0, b"", b"")

    monkeypatch.setattr(integrity.anyio, "run_process", run_process)


def test_sanitizing_a_flac_removes_its_id3_tag(fake_flac_reencode, tmp_path) -> None:
    path = tmp_path / "01.flac"
    path.write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 300 + b"fLaC" + b"\x00" * 300)
    assert has_id3_tag(str(path)) is True

    sanitized = anyio.run(integrity.sanitize_integrity, str(path))

    assert sanitized is True
    assert has_id3_tag(str(path)) is False
