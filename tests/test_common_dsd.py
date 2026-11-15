"""salmon.common.dsd: a DSD rate as the encoding RED's form names, and as the PCM rate its spectrals are made at."""

import pytest

from salmon.common.dsd import (
    dsd_encoding,
    dsd_refusal,
    is_dsd,
    is_dsd_codec,
    pcm_rate,
    rate_text,
    release_dsd_encoding,
    track_dsd_encoding,
)


@pytest.mark.parametrize(
    ("rate", "encoding"),
    [
        (2822400, "DSD64"),
        (5644800, "DSD128"),
        (11289600, "DSD256"),
        (22579200, "DSD512"),
        # The 48 kHz family's.
        (3072000, "DSD64"),
        (12288000, "DSD256"),
        # Not a rate RED's form names, or no DSD rate at all.
        (45158400, None),
        (1411200, None),
        (44100, None),
        (None, None),
    ],
)
def test_a_dsd_rate_is_named_as_red_names_it(rate: int | None, encoding: str | None) -> None:
    assert dsd_encoding(rate) == encoding


def _track(rate: int, precision: int | None = 1) -> dict:
    return {"sample rate": rate, "precision": precision}


def test_only_a_1_bit_track_is_dsd() -> None:
    assert track_dsd_encoding(_track(2822400)) == "DSD64"
    # A 24-bit file at a DSD rate would be no DSD.
    assert track_dsd_encoding(_track(2822400, 24)) is None
    assert track_dsd_encoding(_track(44100, 16)) is None


def test_a_release_has_a_dsd_encoding_only_when_every_track_has_the_same() -> None:
    assert release_dsd_encoding({"a": _track(5644800), "b": _track(5644800)}) == "DSD128"
    assert release_dsd_encoding({"a": _track(2822400), "b": _track(5644800)}) is None
    assert release_dsd_encoding({"a": _track(2822400), "b": _track(44100, 16)}) is None
    assert release_dsd_encoding({}) is None


@pytest.mark.parametrize(
    ("rate", "pcm"),
    [
        # Mutagen's rate, and FFmpeg's (an eighth of it), give the same.
        (2822400, 176400),
        (352800, 176400),
        (11289600, 176400),
        (1411200, 176400),
        (3072000, 192000),
        (384000, 192000),
    ],
)
def test_dsd_is_decoded_to_4_times_its_base_rate(rate: int, pcm: int) -> None:
    assert pcm_rate(rate) == pcm


def test_names_codecs_and_rates() -> None:
    assert [is_dsd(name) for name in ("01.dsf", "01.DFF", "01.flac", "dsf")] == [True, True, False, False]
    assert [is_dsd_codec(name) for name in ("dsd_lsbf_planar", "dsd_msbf", "dst", "flac")] == [True] * 3 + [False]
    assert rate_text(11289600) == "11.2896 MHz"


def test_a_tracker_gets_dsd_only_when_it_is_known_to_take_it() -> None:
    assert dsd_refusal("RED", True) is None
    assert dsd_refusal("OPS", False) == "OPS does not take DSD."
    assert dsd_refusal("DIC", None) == "salmon does not know whether DIC takes DSD, so it sends it none."
