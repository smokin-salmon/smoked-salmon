"""Who made these files, and does what they claim match what they are.

Ported from chodeus's fork (`checks/provenance.py`), plus the CD ripper check (#538).

Rippers, stores and resellers stamp their own markers into the tags: 'EAC FLAC -8', 'QOBUZ',
'hd24bit.com'. A marker alone is ordinary and says nothing; one the audio contradicts (a 24bit claim
on a 16bit file, a CD ripper on a 96 kHz file) is worth a look before uploading. This only warns.
"""

import re
from typing import Any

from mutagen.flac import StreamInfo as FlacStreamInfo

from salmon.tagger.tag_urls import field_name, tag_texts, tag_url_fields
from salmon.tagger.tags import gather_tags

# Field names (see tag_urls.field_name, so MP3 and M4A frames count too) of the ripper/store markers worth reading.
MARKER_FIELDS = (
    "comment",
    "encoded-by",
    "encodedby",
    "encoder",
    "encoder settings",
    "source",
    "sourceurl",
    "website",
    "url",
)

# A bare domain has to swallow its port and path too, or "hd24bit.com/24bit"
# leaves "/24bit" behind and the leftover reads as a claim about the audio.
_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[\w-]+\.(?:com|net|org|io|co|me|ru|to|cc|sh)\b(?::\d+)?(?:[/?#]\S*)?",
    re.IGNORECASE,
)
_DEPTH_CLAIM_RE = re.compile(r"(\d{2})\s*-?\s*bit", re.IGNORECASE)

# Programs that only rip CDs, so their marker means the audio was 16 bit / 44.1 kHz when it was made.
# XLD, dBpoweramp, CUETools, fre:ac and EZ CD Audio Converter are left out on purpose: they also convert
# downloaded files, and their marker on a clean hi-res WEB release would be a false alarm.
_CD_RIPPER_RE = re.compile(r"\b(?:exact\s+audio\s+copy|eac|whipper|morituri|rubyripper|cueripper)\b", re.IGNORECASE)
_CD_DEPTH = 16
_CD_RATE = 44100


def _lossless_depth(info: Any) -> int | None:
    """The bit depth of a FLAC or ALAC file; None for a lossy one, whose depth says nothing about its source."""
    if isinstance(info, FlacStreamInfo) or getattr(info, "codec", None) == "alac":
        return getattr(info, "bits_per_sample", None) or None
    return None


def _file_provenance(filename: str, tagfile: Any) -> dict[str, Any]:
    """Vendor string, marker tags and the file's real bit depth and sample rate."""
    mut = getattr(tagfile, "mut", None)
    tags = getattr(mut, "tags", None)
    markers: dict[str, str] = {}
    if tags is not None:
        for key, value in dict(tags).items():
            field = field_name(key)
            text = "; ".join(t for t in tag_texts(value) if t)
            if field in MARKER_FIELDS and text:
                markers[field] = f"{markers[field]}; {text}" if field in markers else text
        for field, url in tag_url_fields(mut):
            markers.setdefault(field, url)
    info = getattr(mut, "info", None)
    return {
        "file": filename,
        "vendor": getattr(tags, "vendor", None),
        "markers": markers,
        "bitdepth": _lossless_depth(info),
        "samplerate": getattr(info, "sample_rate", None) or None,
    }


def _khz(rate: int) -> str:
    return f"{rate / 1000:g}kHz"


def _contradictions(files: list[dict[str, Any]]) -> list[str]:
    """Markers that claim something the audio is not: a bit depth, or a CD rip."""
    found = []
    for entry in files:
        depth, rate = entry["bitdepth"], entry["samplerate"]
        for field, text in entry["markers"].items():
            # A depth inside a domain is part of the name of whoever ripped it
            # ("hd24bit.com"), not an assertion about this file. The URL still
            # shows up as a marker, so nothing is hidden: it just isn't a claim.
            text = _URL_RE.sub(" ", text)
            if depth:
                for claim in _DEPTH_CLAIM_RE.findall(text):
                    if int(claim) != depth:
                        found.append(f"{entry['file']}: {field} claims {claim}bit, the audio is {depth}bit")
            ripper = _CD_RIPPER_RE.search(text)
            if ripper and ((depth and depth != _CD_DEPTH) or (rate and rate != _CD_RATE)):
                audio = "/".join(part for part in (f"{depth}bit" if depth else "", _khz(rate) if rate else "") if part)
                found.append(f"{entry['file']}: {field} names {ripper.group()}, a CD ripper, but the audio is {audio}")
    return found


def gather_provenance(path: str) -> dict[str, Any]:
    """Encoder and source markers across an album, plus any claim the audio contradicts."""
    try:
        tags = gather_tags(path)
    except Exception:
        return {"files": [], "vendors": [], "markers": [], "urls": [], "contradictions": []}

    files = [_file_provenance(name, tagfile) for name, tagfile in tags.items()]
    markers = {f"{field}: {text}" for entry in files for field, text in entry["markers"].items()}
    urls = {match for entry in files for text in entry["markers"].values() for match in _URL_RE.findall(text)}
    return {
        "files": files,
        "vendors": sorted({entry["vendor"] for entry in files if entry["vendor"]}),
        "markers": sorted(markers),
        "urls": sorted(urls),
        "contradictions": _contradictions(files),
    }
