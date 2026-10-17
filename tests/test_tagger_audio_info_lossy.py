"""A lossy file has no bit depth, though mutagen reports one for AAC in an MP4."""

import pytest
from mutagen.mp4 import MP4Info

from salmon.tagger.audio_info import _parse_audio_info
from salmon.tagger.foldername import resolution_token


def _mp4_info(codec: str) -> MP4Info:
    """An MP4 stream as mutagen describes it: AAC and ALAC alike carry the sample entry's 16 bits."""
    info = MP4Info.__new__(MP4Info)
    info.codec, info.bits_per_sample, info.sample_rate = codec, 16, 48000
    info.channels, info.bitrate, info.length = 2, 256000, 1.0
    return info


@pytest.mark.parametrize(("codec", "precision"), [("mp4a.40.2", None), ("alac", 16)], ids=["aac", "alac"])
def test_only_a_lossless_mp4_has_a_bit_depth(codec: str, precision: int | None) -> None:
    assert _parse_audio_info(_mp4_info(codec))["precision"] == precision


def test_an_aac_folder_gets_no_resolution_token() -> None:
    assert resolution_token({"01.m4a": _parse_audio_info(_mp4_info("mp4a.40.2"))}) == ""
