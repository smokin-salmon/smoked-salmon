"""DSD (Direct Stream Digital) audio: .dsf and .dff files of 1-bit samples at 64 times a PCM rate or more (#919).

salmon uploads them as they are, with the format DSD and the rate as the encoding (DSD64 to DSD512), to a tracker
that takes them (TagRules.takes_dsd). Nothing here imports more than the standard library: the tagger, the checks and
the uploader all use it.
"""

from collections.abc import Mapping
from typing import Any

DSD_EXTENSIONS = frozenset({".dsf", ".dff"})

# The PCM rates a DSD rate is a multiple of: 64 x 44.1 kHz (2.8224 MHz) is DSD64, as is 64 x 48 kHz (3.072 MHz).
_BASE_RATES = (44100, 48000)
# The multiples RED's upload form names, as DSD64 to DSD512.
_MULTIPLES = (64, 128, 256, 512)

# What the spectrals and the frequency analysis decode DSD to: 4 x its base rate shows up to 88.2 or 96 kHz, enough for
# a PCM master's cutoff and the rise of DSD's shaped noise above the audio band.
_PCM_RATES = {44100: 176400, 48000: 192000}


def is_dsd(filename: str) -> bool:
    """Whether the file is a DSF or DSDIFF file, by its name."""
    return filename.lower().endswith(tuple(DSD_EXTENSIONS))


def is_dsd_codec(name: str) -> bool:
    """Whether FFmpeg's codec of that name decodes DSD: dsd_lsbf, dsd_msbf and their planar forms, or DST (a
    compressed DSDIFF)."""
    return name.startswith("dsd_") or name == "dst"


def dsd_encoding(sample_rate: int | None) -> str | None:
    """The encoding of DSD at sample_rate (mutagen's: 2822400 for DSD64), as RED's form names it; None for another
    rate."""
    for base in _BASE_RATES:
        multiple, rest = divmod(sample_rate or 0, base)
        if not rest and multiple in _MULTIPLES:
            return f"DSD{multiple}"
    return None


def track_dsd_encoding(track: Mapping[str, Any]) -> str | None:
    """The DSD encoding of a gather_audio_info entry; None for a PCM or lossy track."""
    return dsd_encoding(track.get("sample rate")) if track.get("precision") == 1 else None


def release_dsd_encoding(audio_info: Mapping[str, Mapping[str, Any]]) -> str | None:
    """The DSD encoding every track has; None if they have more than one, or one that is not a DSD rate."""
    encodings = {track_dsd_encoding(track) for track in audio_info.values()}
    if len(encodings) == 1:
        return encodings.pop()
    return None


def pcm_rate(rate: int) -> int:
    """The PCM rate DSD at rate is decoded to for its spectrals. rate may be the DSD rate, or FFmpeg's stream rate,
    an eighth of it: both are multiples of the same base rate."""
    if rate % 48000 == 0 and rate % 44100:
        return _PCM_RATES[48000]
    return _PCM_RATES[44100]


def rate_text(sample_rate: int) -> str:
    """A DSD rate in MHz: "11.2896 MHz"."""
    return f"{sample_rate / 1_000_000:g} MHz"


def dsd_refusal(tracker: str, takes_dsd: bool | None) -> str | None:
    """Why a DSD release does not go to the tracker, from its TagRules.takes_dsd; None if it takes DSD."""
    if takes_dsd:
        return None
    if takes_dsd is None:
        return f"salmon does not know whether {tracker} takes DSD, so it sends it none."
    return f"{tracker} does not take DSD."
