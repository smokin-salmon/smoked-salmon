"""Fix or warn about tagging problems that would otherwise go unnoticed until upload.

An ID3 tag inside a FLAC is stripped in place, the same way an oversized embedded picture is
stripped by default; a scene release is never touched, so it only gets a warning instead. An
uncompressed FLAC and an MP3 with a dual ID3 tag set are only ever warned about. Blocking checks
(folder structure, decode integrity, required tags) live elsewhere.
"""

import os

from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import ID3

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


def _id3v1_holds_text(filepath: str) -> bool:
    """Whether the file ends in an ID3v1 block with anything in its text fields."""
    with open(filepath, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() < 128:
            return False
        handle.seek(-128, os.SEEK_END)
        block = handle.read(128)
    # Byte 126 is the ID3v1.1 track number when byte 125 is zero, else the comment's end.
    text = block[3:125] if block[125] == 0 else block[3:127]
    return block[:3] == b"TAG" and bool(text.strip(b"\0 "))


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

    Raises:
        MutagenError, OSError: If the ID3v2 tag cannot be read: mutagen raises the same error for a
            malformed tag and a failed read, so neither is taken for a clean file.
    """
    if not (_id3v1_holds_text(filepath) and _has_id3v2_header(filepath)):
        return False
    # load_v1=False: mutagen otherwise reads the v1 fields into the v2 tag, and a blank v2 looks filled.
    tags = ID3(filepath, load_v1=False)
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


def _id3_in_flac(filename: str) -> str:
    return f"{filename}: FLAC file contains an ID3 tag (RED and OPS do not allow ID3 tags in FLAC files)."


def _uncompressed(filename: str) -> str:
    return f"{filename}: FLAC file looks uncompressed (RED and OPS can trump it); recompress it with salmon up -c."


def _dual_id3(filename: str, filepath: str) -> str | None:
    """The line for an MP3 with a dual ID3 tag set, or for one whose ID3v2 tag cannot be read; None if neither."""
    try:
        dual = has_blank_id3v2_alongside_id3v1(filepath)
    except (MutagenError, OSError) as e:
        return f"{filename}: could not read its ID3v2 tag ({e}); check it by hand."
    if dual:
        return f"{filename}: MP3 file has a filled-in ID3v1 tag and a blank ID3v2 tag (RED and OPS can trump it)."
    return None


def find_tag_issues(path: str, audio_info: dict) -> list[str]:
    """The tagging problems process_tag_issues fixes or warns about, without fixing any: it changes no file.

    Args:
        path: Path to the release folder.
        audio_info: Mapping of filename to the technical info `gather_audio_info` collects.

    Returns:
        One line per problem found; empty when there is none.
    """
    messages: list[str] = []
    for filename in get_audio_files(path):
        filepath = os.path.join(path, filename)
        lower = filename.lower()
        if lower.endswith(".flac"):
            if has_id3_tag(filepath):
                messages.append(_id3_in_flac(filename))
            track = audio_info.get(filename)
            if track and is_uncompressed(track):
                messages.append(_uncompressed(filename))
        elif lower.endswith(".mp3") and (message := _dual_id3(filename, filepath)):
            messages.append(message)
    return messages


def process_tag_issues(path: str, audio_info: dict, *, scene: bool, recompress: bool) -> list[str]:
    """Fix or warn about the tagging problems RED and OPS can act on.

    A FLAC's ID3 tag is stripped in place, unless this is a scene release, which is never touched
    and only gets a warning. An uncompressed FLAC is not worth warning about when the release is
    about to be recompressed anyway (recompress). Neither the uncompressed-FLAC nor the dual-ID3
    MP3 case is ever fixed automatically, only warned about.

    Args:
        path: Path to the release folder.
        audio_info: Mapping of filename to the technical info `gather_audio_info` collects.
        scene: Whether this is a scene release: its files are never modified.
        recompress: Whether the release is about to be recompressed, which also fixes an
            uncompressed FLAC.

    Returns:
        Lines to print, one per file fixed or warned about; empty when nothing to report.
    """
    messages: list[str] = []
    for filename in get_audio_files(path):
        filepath = os.path.join(path, filename)
        lower = filename.lower()
        if lower.endswith(".flac"):
            if has_id3_tag(filepath):
                if scene:
                    messages.append(_id3_in_flac(filename))
                else:
                    try:
                        flac = FLAC(filepath)
                        padding = sum(block.length for block in flac.metadata_blocks if block.code == 1)
                        # The padding the file had: the bytes the ID3 tag took go, rather than becoming padding.
                        flac.save(deleteid3=True, padding=lambda _info, keep=padding: keep)
                    except (MutagenError, OSError) as e:
                        messages.append(f"{filename}: could not remove its ID3 tag ({e}); remove it by hand.")
                    else:
                        messages.append(
                            f"Removed an ID3 tag from {filename} (RED and OPS do not allow ID3 tags in FLAC files)."
                        )
            if not recompress:
                track = audio_info.get(filename)
                if track and is_uncompressed(track):
                    messages.append(_uncompressed(filename))
        elif lower.endswith(".mp3") and (message := _dual_id3(filename, filepath)):
            messages.append(message)
    return messages
