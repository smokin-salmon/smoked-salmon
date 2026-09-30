"""Read the URLs a release's own files carry in their tags.

Ported from chodeus's fork (`checks/source.py`), narrowed to just the tag-URL reading that
`tagger/metadata.py` needs to find the files' Deezer album (#545). The media/log source detection
that lived alongside it in the fork is out of scope here.

Kept as its own module so the source detection (`checks/source.py`, #537) and the metadata
pre-fill work (#536) import this instead of re-porting it.
"""

import os
import re

from mutagen import File as MutagenFile
from mutagen.mp4 import AtomDataType, MP4FreeForm

from salmon.common.files import get_audio_files

# Keys downloaders use for the page the files came from; WOAS is ID3's "official audio source".
_SOURCE_KEYS = frozenset({"source", "sourceurl", "www", "website", "url", "purl", "woas"})
# MusicBrainz and Discogs links describe a release in a database, not where these files came from:
# skip both their keys (which can hold a store link) and their own URLs under any key.
_DATABASE_KEYS = ("musicbrainz", "discogs")
_DATABASE_URL = re.compile(r"https?://(?:[a-z0-9-]+\.)*(?:musicbrainz\.org|discogs\.com)(?:[/:?#]|$)", re.IGNORECASE)
_URL_VALUE = re.compile(r"https?://\S+", re.IGNORECASE)
# ID3 and MP4 frames that hold a field under another name.
_FRAME_FIELDS = {
    "comm": "comment",
    "tenc": "encoded-by",
    "tsse": "encoder settings",
    "\xa9cmt": "comment",
    "\xa9too": "encoder",
}


def field_name(key) -> str:
    """Lowercase Vorbis-style name for a tag key from any container (TXXX:SOURCE, COMM::eng, ----:…:MEDIA)."""
    name = str(key).lower()
    if name.startswith(("txxx:", "wxxx:")):
        return name[5:]
    if name.startswith("----:"):
        return name.rsplit(":", 1)[-1]
    return _FRAME_FIELDS.get(name.split(":", 1)[0], name)


def _decode_bytes(item: bytes) -> str:
    """Decode a raw tag value: an MP4 freeform's own `dataformat` when it says UTF-16, else UTF-8.

    iTunes writes UTF-16 freeform data big-endian with no BOM; only trust the "utf-16" codec's own
    byte-order guess when a BOM is actually present, otherwise decode it as big-endian explicitly.
    """
    if isinstance(item, MP4FreeForm) and item.dataformat == AtomDataType.UTF16:
        encoding = "utf-16" if item[:2] in (b"\xfe\xff", b"\xff\xfe") else "utf-16-be"
        return item.decode(encoding, "ignore")
    return item.decode("utf-8", "ignore")


def tag_texts(value) -> list[str]:
    """A tag value as plain strings, whether a list, an ID3 frame or MP4 freeform bytes."""
    items = value if isinstance(value, list) else [value]
    return [(_decode_bytes(item) if isinstance(item, bytes) else str(item)).strip() for item in items]


def tag_pairs(mut) -> list:
    """The file's (key, value) tag pairs, database links left out."""
    pairs = dict(mut.tags or {}).items()
    return [(key, value) for key, value in pairs if not field_name(key).startswith(_DATABASE_KEYS)]


def tag_url_fields(mut) -> list[tuple[str, str]]:
    """(field name, URL) for every tag whose whole value is one URL."""
    return [
        (field_name(key), text)
        for key, value in tag_pairs(mut)
        for text in tag_texts(value)
        if _URL_VALUE.fullmatch(text) and not _DATABASE_URL.match(text)
    ]


def tag_urls(path: str) -> tuple[list[str], list[str]]:
    """URLs the files' tags hold whole: (under a source key such as SOURCE or WOAS, under any other key)."""
    sourced, other = [], []
    for filename in get_audio_files(path, True):
        try:
            mut = MutagenFile(os.path.join(path, filename))
        except Exception:
            continue
        if mut is None:
            continue
        for field, url in tag_url_fields(mut):
            (sourced if field in _SOURCE_KEYS else other).append(url)
    return list(dict.fromkeys(sourced)), list(dict.fromkeys(other))
