"""A source that cannot be transcoded is refused before the transcode writes or deletes anything."""

import os
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from salmon.converter import transcoding as tc


def _streaminfo_only_flac() -> bytes:
    """A FLAC of 16bit/44.1kHz with no audio: the output path reads the source's resolution."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    return b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo


def _source(folder: Path) -> Path:
    folder.mkdir()
    (folder / "01.flac").write_bytes(_streaminfo_only_flac())
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


def _six_channels(monkeypatch: pytest.MonkeyPatch, source: Path) -> None:
    """The source's one track as a 5.1 file; only info.channels is read before the refusal."""
    fake: Any = SimpleNamespace(info=SimpleNamespace(channels=6))
    monkeypatch.setattr(
        tc,
        "_collect_transcode_items",
        lambda _path, new_path: [
            tc.TranscodeItem(src=str(source / "01.flac"), dst=f"{new_path}/01.mp3", tags={}, flac_obj=fake)
        ],
    )


def test_a_multichannel_source_is_refused_before_a_prior_output_is_deleted(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path / "Artist - Album (2024) [FLAC]")
    existing = Path(tc._build_output_path(str(source), "320"))
    existing.mkdir()
    (existing / "01 - already here.mp3").write_bytes(b"prior output")
    _six_channels(monkeypatch, source)

    with pytest.raises(ValueError, match="6 channels"):
        anyio.run(lambda: tc.transcode_folder(str(source), "320"))

    assert (existing / "01 - already here.mp3").read_bytes() == b"prior output"


def test_a_multichannel_source_writes_no_output_folder(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path / "Artist - Other (2024) [FLAC]")
    _six_channels(monkeypatch, source)

    with pytest.raises(ValueError, match="6 channels"):
        anyio.run(lambda: tc.transcode_folder(str(source), "320"))

    # The extra files are copied before any track is encoded.
    assert not os.path.exists(tc._build_output_path(str(source), "320"))
