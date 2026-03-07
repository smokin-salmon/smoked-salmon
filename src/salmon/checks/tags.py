"""Validate file-level tags against tracker-specific upload rules.

Runs as part of the upload flow to warn about tagging issues that could
cause a release to be flagged as a dupe or become trumpable.
"""

import os
import re
from datetime import datetime
from typing import Any

import mutagen
import mutagen.flac
import mutagen.id3
from mutagen import File as MutagenFile

from salmon.common import get_audio_files
from salmon.tagger.tagfile import TagFile
from salmon.trackers.base import TagRules


def validate_tags_for_tracker(
    path: str,
    tags: dict[str, TagFile],
    audio_info: dict[str, dict[str, Any]],
    tag_rules: TagRules,
) -> list[str]:
    """Validate file-level tags against tracker rules.

    Returns a list of warning strings. Empty list means all checks passed.
    """
    warnings: list[str] = []
    warnings.extend(_check_required_tags(tags))
    warnings.extend(_check_flac_tag_format(path, tag_rules))
    warnings.extend(_check_tag_content(tags))
    warnings.extend(_check_dual_id3(path))
    warnings.extend(_check_embedded_images(path))
    warnings.extend(_check_file_naming(path))
    warnings.extend(_check_path_length(path, tag_rules))
    warnings.extend(_check_tracker_specific(audio_info, tag_rules))
    return warnings


REQUIRED_TAGS = ("artist", "album", "title", "tracknumber")


def _check_required_tags(tags: dict[str, TagFile]) -> list[str]:
    warnings: list[str] = []
    for filename, tag in tags.items():
        missing = [t for t in REQUIRED_TAGS if not getattr(tag, t, False)]
        if missing:
            warnings.append(f"{filename}: missing required tags: {', '.join(missing)}")
    return warnings


def _check_flac_tag_format(path: str, tag_rules: TagRules) -> list[str]:
    warnings: list[str] = []
    for filename in get_audio_files(path):
        filepath = os.path.join(path, filename)
        mut = MutagenFile(filepath)
        if not isinstance(mut, mutagen.flac.FLAC):
            continue

        # Check for ID3 tags on FLAC
        try:
            id3_tags = mutagen.id3.ID3(filepath)
            if id3_tags:
                warnings.append(f"{filename}: FLAC file contains ID3 tags")
        except mutagen.id3.ID3NoHeaderError:
            pass

        # Check TRACKNUMBER field name (RED requires TRACKNUMBER, not TRACK)
        if tag_rules.require_tracknumber_field:
            vorbis = mut.tags
            if vorbis is not None and "TRACK" in vorbis and "TRACKNUMBER" not in vorbis:
                warnings.append(
                    f"{filename}: uses 'TRACK' instead of 'TRACKNUMBER' Vorbis comment"
                )

    return warnings


def _check_tag_content(tags: dict[str, TagFile]) -> list[str]:
    warnings: list[str] = []
    for filename, tag in tags.items():
        title = getattr(tag, "title", "") or ""
        album = getattr(tag, "album", "") or ""

        # All-caps detection — commented out: can be artistic intent (RED 2.3.18.2, OPS 2.3.18)
        # TODO: revisit with a way to distinguish artistic choice from bad tagging
        # artist = getattr(tag, "artist", "") or ""
        # if title and len(title) > 3 and title == title.upper() and title != title.lower():
        #     warnings.append(f"{filename}: title tag is all caps: '{title}'")
        # if artist and len(artist) > 3 and artist == artist.upper() and artist != artist.lower():
        #     warnings.append(f"{filename}: artist tag is all caps: '{artist}'")
        # if album and len(album) > 3 and album == album.upper() and album != album.lower():
        #     warnings.append(f"{filename}: album tag is all caps: '{album}'")

        # Track number stuffed in title (e.g. "01 - Song Name" or "01. Song Name")
        if re.match(r"^\d+\s*[-\.]\s+", title):
            warnings.append(f"{filename}: title tag appears to contain track number: '{title}'")

        # Disc number in album tag (e.g. "Album Disc 2", "Album CD1")
        if re.search(r"\b(disc|cd)\s*\d+\b", album, re.IGNORECASE):
            warnings.append(f"{filename}: album tag may contain disc number: '{album}'")

        # Suspicious year value
        year = getattr(tag, "date", "") or ""
        if year:
            try:
                y = int(str(year)[:4])
                if y < 1860 or y > datetime.now().year + 1:
                    warnings.append(f"{filename}: suspicious year tag: '{year}'")
            except ValueError:
                pass

    return warnings


def _check_dual_id3(path: str) -> list[str]:
    warnings: list[str] = []
    for filename in get_audio_files(path):
        filepath = os.path.join(path, filename)
        if not filepath.lower().endswith(".mp3"):
            continue
        try:
            id3_tags = mutagen.id3.ID3(filepath)
        except mutagen.id3.ID3NoHeaderError:
            continue
        has_v2_content = any(id3_tags.getall(key) for key in id3_tags)
        has_v1 = id3_tags.version[0] == 1
        if has_v1 and not has_v2_content:
            warnings.append(f"{filename}: has ID3v1 tags with blank ID3v2 (dual tag set)")
    return warnings


def _check_embedded_images(path: str) -> list[str]:
    warnings: list[str] = []
    max_bytes = 1024 * 1024  # 1024 KiB
    for filename in get_audio_files(path):
        filepath = os.path.join(path, filename)
        mut = MutagenFile(filepath)
        if mut is None:
            continue
        pic_size = 0
        if isinstance(mut, mutagen.flac.FLAC):
            for pic in mut.pictures:
                pic_size += len(pic.data)
            if mut.metadata_blocks:
                for block in mut.metadata_blocks:
                    if isinstance(block, mutagen.flac.Padding):
                        pic_size += block.length
        elif hasattr(mut, "tags") and isinstance(mut.tags, mutagen.id3.ID3):
            for key in mut.tags:
                if key.startswith("APIC"):
                    pic_size += len(mut.tags[key].data)
        if pic_size > max_bytes:
            warnings.append(
                f"{filename}: embedded image + padding is {pic_size // 1024} KiB (max 1024 KiB)"
            )
    return warnings


def _check_file_naming(path: str) -> list[str]:
    warnings: list[str] = []
    audio_files = get_audio_files(path)

    for filename in audio_files:
        basename_with_ext = os.path.basename(filename)
        basename = os.path.splitext(basename_with_ext)[0]

        # Leading spaces in filename
        if basename_with_ext != basename_with_ext.lstrip():
            warnings.append(f"{filename}: file name has leading spaces")

        # Track number missing from filename
        # Allow numeric (01, 1) or vinyl-style (A1, B2)
        if not re.match(r"^\d+", basename.lstrip()):
            if not re.match(r"^[A-Z]\d+", basename.lstrip(), re.IGNORECASE):
                warnings.append(f"{filename}: file name missing track number")

    # Check folder name for leading spaces
    folder_name = os.path.basename(path)
    if folder_name != folder_name.lstrip():
        warnings.append(f"Folder name has leading spaces: '{folder_name}'")

    # Multi-disc: check for duplicate track numbers in same directory
    dirs: dict[str, list[str]] = {}
    for filename in audio_files:
        full = os.path.join(path, filename)
        d = os.path.dirname(full)
        dirs.setdefault(d, []).append(os.path.basename(filename))

    for d, files in dirs.items():
        track_nums: list[str] = []
        for f in files:
            m = re.match(r"^(\d+)", f)
            if m:
                track_nums.append(m.group(1))
        dupes = [n for n in set(track_nums) if track_nums.count(n) > 1]
        if dupes:
            rel_dir = os.path.relpath(d, path) or "."
            warnings.append(
                f"Duplicate track numbers in '{rel_dir}': {', '.join(sorted(dupes))}"
            )

    # VA sort order: artist appears before track number
    for filename in audio_files:
        basename = os.path.splitext(os.path.basename(filename))[0]
        if re.match(r"^[^0-9].*\s*-\s*\d+\s*-\s*", basename):
            warnings.append(
                f"{filename}: artist appears before track number "
                "(should be '## - Artist - Title')"
            )
            break  # One warning is enough

    return warnings


def _check_path_length(path: str, tag_rules: TagRules) -> list[str]:
    warnings: list[str] = []
    max_len = tag_rules.max_path_length
    folder_name = os.path.basename(path)

    for root, _, files in os.walk(path):
        for f in files:
            rel_path = os.path.join(
                folder_name, os.path.relpath(os.path.join(root, f), path)
            )
            if len(rel_path) > max_len:
                warnings.append(
                    f"Path too long ({len(rel_path)} > {max_len} chars): {rel_path}"
                )
    return warnings


def _check_tracker_specific(
    audio_info: dict[str, dict[str, Any]], tag_rules: TagRules
) -> list[str]:
    warnings: list[str] = []
    allowed_rates = tag_rules.allowed_sample_rates
    max_16bit_rate = tag_rules.max_16bit_sample_rate

    if not allowed_rates and not max_16bit_rate:
        return warnings

    for filename, info in audio_info.items():
        rate = info.get("sample rate", 0)
        bit_depth = info.get("precision", 0)

        if allowed_rates and rate not in allowed_rates:
            warnings.append(
                f"{filename}: sample rate {rate} Hz not allowed "
                f"(must be one of: {', '.join(str(r) for r in sorted(allowed_rates))})"
            )

        if max_16bit_rate and bit_depth == 16 and rate > max_16bit_rate:
            warnings.append(
                f"{filename}: 16-bit audio at {rate} Hz not allowed "
                f"(max {max_16bit_rate} Hz for 16-bit)"
            )

    return warnings
