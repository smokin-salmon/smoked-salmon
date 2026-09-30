"""Detect an album's media source from its own files (#537).

Ported from chodeus's fork (`checks/source.py`). A source is reported only when the files prove it
and nothing in them disagrees: a plain 16/44.1 rip with no log could be a CD or a WEB release, so it
stays a question. The result is only ever offered as the source prompt's default.

Reads the files already in the folder and nothing else.
"""

import os
import re
from dataclasses import dataclass

from mutagen import File as MutagenFile

from salmon.common.files import get_audio_files
from salmon.tagger.tag_urls import field_name, tag_pairs, tag_texts, tag_url_fields

# Enough of a log to hold the ripper's banner.
_LOG_HEAD_BYTES = 4096
# CD rippers only: a verification log (CUETools, an AccurateRip check) can be made from any files.
_RIPPERS = re.compile(r"exact audio copy|x lossless decoder|whipper|morituri|dbpoweramp|cueripper", re.IGNORECASE)
# Fields only a store writes. Not ASIN (Picard copies it from MusicBrainz), and not the
# com.apple.iTunes namespace as such (every tagger files custom M4A fields under it).
_ITUNES_PURCHASE_FIELDS = frozenset({"apid", "purd", "purchase date", "purchase_date", "purchasedate"})
# The comment Bandcamp writes into its downloads.
_BANDCAMP_COMMENT = re.compile(r"visit https?://[a-z0-9-]+\.bandcamp\.com\b")
# Apple only by its music stores: the rest of apple.com sells no albums.
_STORE_URL = re.compile(
    r"https?://(?:(?:[a-z0-9-]+\.)*(qobuz|deezer|tidal|bandcamp|beatport|7digital|hdtracks)|(?:music|itunes)\.(apple))"
    r"\.com(?:[/:?#]|$)",
    re.IGNORECASE,
)
_STORE_NAMES = {
    "qobuz": "Qobuz",
    "deezer": "Deezer",
    "tidal": "Tidal",
    "bandcamp": "Bandcamp",
    "beatport": "Beatport",
    "7digital": "7digital",
    "hdtracks": "HDtracks",
    "apple": "Apple",
}
_MEDIA_FIELDS = frozenset({"media", "sourcemedia", "tmed"})
# A media tag's whole value, so that "CD/Vinyl" names no source.
_MEDIA_VALUES = {
    "cd": "CD",
    "compact disc": "CD",
    "web": "WEB",
    "digital media": "WEB",
    "file": "WEB",
    "digital": "WEB",
    "vinyl": "Vinyl",
    "lp": "Vinyl",
    '7" vinyl': "Vinyl",
    '10" vinyl': "Vinyl",
    '12" vinyl': "Vinyl",
    "cassette": "Cassette",
    "sacd": "SACD",
    "dvd": "DVD",
}
_VINYL_SIDE = re.compile(r"[A-H][0-9]{1,2}", re.IGNORECASE)
# What narrowing evidence leaves possible. Side numbering is also kept by the WEB release of a vinyl
# album, and a cassette has sides too.
_VINYL_SIDE_SOURCES = frozenset({"Vinyl", "Cassette", "WEB"})


@dataclass(frozen=True)
class DetectedSource:
    source: str
    """A value of `constants.SOURCES`, so it is always a valid answer to the source prompt."""
    reason: str
    """Why, for the user: "rip log found (album.log)", "Qobuz URL in the tags"."""


@dataclass
class _Evidence:
    proofs: dict[str, str]
    """Reason -> the source it proves."""
    tracknumbers: list[str]
    above_cd_quality: bool


def detect_source(path: str) -> DetectedSource | None:
    """The media source the album's files prove, or None when they do not prove one.

    A rip log, a store URL in the tags, a tag only a store writes or a media tag each prove a
    source. Vinyl side numbering and a rate above CD's 16/44.1 only rule sources out: alone they
    prove nothing, but they make a proof they contradict unknown. So does a second proof that
    names another source.
    """
    evidence = _gather(path)
    sources = set(evidence.proofs.values())
    if len(sources) != 1:
        return None
    source = sources.pop()
    if evidence.above_cd_quality and source == "CD":
        return None
    if _vinyl_sides(evidence.tracknumbers) and source not in _VINYL_SIDE_SOURCES:
        return None
    return DetectedSource(source, "; ".join(evidence.proofs))


def _gather(path: str) -> _Evidence:
    evidence = _Evidence(proofs={}, tracknumbers=[], above_cd_quality=False)
    if log := _rip_log(path):
        evidence.proofs[f"rip log found ({log})"] = "CD"
    for filename in get_audio_files(path):
        try:
            mut = MutagenFile(os.path.join(path, filename))
        except Exception:  # A truncated or corrupt file must not sink the whole scan.
            continue
        if mut is None:
            continue
        evidence.proofs.update(_tag_proofs(mut))
        evidence.tracknumbers.extend(_tracknumbers(mut))
        bits = getattr(mut.info, "bits_per_sample", None) or 0
        rate = getattr(mut.info, "sample_rate", None) or 0
        evidence.above_cd_quality |= bits > 16 or rate > 44100
    return evidence


def _rip_log(path: str) -> str | None:
    """The name of the first CD ripper's log in the folder, if there is one."""
    for root, _dirs, files in sorted(os.walk(path)):
        for name in sorted(files):
            if not name.lower().endswith(".log"):
                continue
            try:
                with open(os.path.join(root, name), "rb") as fh:
                    head = fh.read(_LOG_HEAD_BYTES)
            except OSError:
                continue
            # EAC writes its logs in UTF-16 with a byte order mark.
            encoding = "utf-16" if head[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
            if _RIPPERS.search(head.decode(encoding, "ignore")):
                return name
    return None


def _tag_proofs(mut) -> dict[str, str]:
    """Reason -> source, for what one file's tags prove."""
    proofs = {}
    for key, value in tag_pairs(mut):
        field = field_name(key)
        for text in tag_texts(value):
            lowered = text.lower()
            if field in _MEDIA_FIELDS and (media := _MEDIA_VALUES.get(lowered)):
                proofs[f'media tag says "{text}"'] = media
            elif field in _ITUNES_PURCHASE_FIELDS:
                proofs["iTunes purchase tags"] = "WEB"
            elif "amazon.com song id" in lowered:
                proofs["Amazon download comment in the tags"] = "WEB"
            elif field == "comment" and _BANDCAMP_COMMENT.match(lowered):
                proofs["Bandcamp comment in the tags"] = "WEB"
    for _field, url in tag_url_fields(mut):
        if match := _STORE_URL.match(url):
            store = (match.group(1) or match.group(2)).lower()
            proofs[f"{_STORE_NAMES[store]} URL in the tags"] = "WEB"
    return proofs


def _tracknumbers(mut) -> list[str]:
    return [
        text.split("/", 1)[0].strip()
        for key, value in tag_pairs(mut)
        if field_name(key) in ("tracknumber", "trck")
        for text in tag_texts(value)
    ]


def _vinyl_sides(tracknumbers: list[str]) -> bool:
    """True when most track numbers are vinyl sides (A1, B2 ...)."""
    if len(tracknumbers) < 2:
        return False
    return sum(bool(_VINYL_SIDE.fullmatch(t)) for t in tracknumbers) >= len(tracknumbers) * 0.8
