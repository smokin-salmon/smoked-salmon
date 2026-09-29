"""Warn about tagging problems that would otherwise go unnoticed until upload.

None of the checks here block an upload; they only print a warning for the user to act on.
Blocking checks (folder structure, decode integrity, required tags) live elsewhere.
"""

import os

from mutagen.id3 import ID3, ID3NoHeaderError

from salmon.common import get_audio_files

# A FLAC storing verbatim, uncompressed frames still leaves a small margin below the raw PCM
# rate for the frame headers' own overhead.
UNCOMPRESSED_RATIO = 0.99


def _has_id3v1_block(filepath: str) -> bool:
    with open(filepath, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() < 128:
            return False
        handle.seek(-128, os.SEEK_END)
        return handle.read(3) == b"TAG"


def _has_id3v2_header(filepath: str) -> bool:
    with open(filepath, "rb") as handle:
        return handle.read(3) == b"ID3"


def has_id3_tag(filepath: str) -> bool:
    """True when an ID3v2 header sits at the start of the file, or an ID3v1 block at its end.

    Ordinary for an MP3, which keeps its tags this way; not for a FLAC, which keeps its metadata
    in Vorbis comments and FLAC metadata blocks instead. A cheap probe: it reads a few bytes from
    each end of the file rather than parsing it fully.
    """
    return _has_id3v2_header(filepath) or _has_id3v1_block(filepath)


def has_blank_id3v2_alongside_id3v1(filepath: str) -> bool:
    """True for an MP3 with a filled-in ID3v1 tag next to an ID3v2 tag that carries no frames.

    Reading only the ID3v1 tag, or only a filled-in ID3v2 tag, is ordinary and not flagged; nor is
    an ID3v2 tag with real content sitting next to an ID3v1 tag. When both a v1 tag and a v2
    header exist, mutagen reads the v2 tag: it is the blank one that matters here, not the
    presence of v1 by itself, so both a v1 block and a v2 header must be found on disk first.
    """
    if not (_has_id3v1_block(filepath) and _has_id3v2_header(filepath)):
        return False
    try:
        tags = ID3(filepath)
    except ID3NoHeaderError:
        return False
    return not any(tags.getall(key) for key in tags)


def is_uncompressed(track: dict) -> bool:
    """True when the file's reported audio bit rate is within a per cent of the raw PCM rate.

    mutagen measures the bit rate from the audio frames alone, after the metadata blocks, so
    embedded pictures and padding are already excluded from it: no need to subtract them again.
    """
    rate = track.get("sample rate")
    bits = track.get("precision")
    channels = track.get("channels")
    bit_rate = track.get("bit rate")
    if not (rate and bits and channels and bit_rate):
        return False
    return bit_rate >= UNCOMPRESSED_RATIO * rate * bits * channels


def in_torrent_path(folder_name: str, relative_path: str) -> str:
    """The path exactly as it sits inside the torrent: the release folder, any sub-folders, and the file.

    Used to measure path length the same way everywhere it matters, whether the result blocks the
    upload or only informs an advisory.
    """
    if relative_path in ("", "."):
        return folder_name
    return f"{folder_name}/{relative_path}"


def collect_tag_warnings(path: str, audio_info: dict) -> list[str]:
    """Warnings for tagging problems RED and OPS can act on, gathered from the files on disk.

    Args:
        path: Path to the release folder.
        audio_info: Mapping of filename to the technical info `gather_audio_info` collects.

    Returns:
        Human-readable warning strings; empty when nothing to report.
    """
    warnings: list[str] = []
    for filename in get_audio_files(path):
        filepath = os.path.join(path, filename)
        lower = filename.lower()
        if lower.endswith(".flac"):
            if has_id3_tag(filepath):
                warnings.append(
                    f"{filename}: FLAC file contains an ID3 tag "
                    "(RED and OPS do not allow ID3 tags in FLAC files); sanitizing removes it."
                )
            track = audio_info.get(filename)
            if track and is_uncompressed(track):
                warnings.append(
                    f"{filename}: FLAC file looks uncompressed (RED and OPS can trump it); "
                    "recompress it with salmon up -c."
                )
        elif lower.endswith(".mp3") and has_blank_id3v2_alongside_id3v1(filepath):
            warnings.append(
                f"{filename}: MP3 file has a filled-in ID3v1 tag and a blank ID3v2 tag (RED and OPS can trump it)."
            )
    return warnings
