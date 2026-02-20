import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import anyio
import asyncclick as click
import msgspec
from mutagen import flac, mp3
from mutagen.flac import VCFLACDict
from mutagen.id3 import APIC, TXXX, Frames

from salmon.common.files import process_files

Bitrate = Literal["V0", "320"]

# LAME encoding presets
LAME_COMMAND_MAP: dict[Bitrate, list[str]] = {
    "V0": ["-V", "0", "--vbr-new"],
    "320": ["-h", "-b", "320"],
}

COPY_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".pdf", ".txt"))
FLAC_FOLDER_RE = re.compile(r"\[FLAC.*\]")
LOSSLESS_FOLDER_RE = re.compile(r"Lossless", flags=re.IGNORECASE)

# Vorbis comment → ID3v2 frame mapping
VORBIS_TO_ID3_MAP: dict[str, str] = {
    "title": "TIT2",
    "album": "TALB",
    "artist": "TPE1",
    "albumartist": "TPE2",
    "album artist": "TPE2",
    "conductor": "TPE3",
    "remixer": "TPE4",
    "composer": "TCOM",
    "tracknumber": "TRCK",
    "discnumber": "TPOS",
    "date": "TDRC",
    "comment": "COMM",
    "genre": "TCON",
    "language": "TLAN",
    "key": "TKEY",
    "bpm": "TBPM",
    "publisher": "TPUB",
    "label": "TPUB",
    "isrc": "TSRC",
}


class TranscodeItem(msgspec.Struct, frozen=True):
    """A FLAC file to be transcoded to MP3."""

    src: str
    dst: str
    flac_obj: flac.FLAC
    tags: dict[str, list[str]]


# Track number / disc number total merging map
_TOT_MAP: dict[str, frozenset[str]] = {
    "tracknumber": frozenset({"tracktotal", "totaltracks", "total tracks"}),
    "discnumber": frozenset({"disctotal", "totaldiscs", "total discs"}),
}


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _build_output_path(path: str, bitrate: Bitrate) -> str:
    """Generate the output directory path for a transcoded release.

    Args:
        path: Source album directory path.
        bitrate: Target MP3 bitrate label (e.g. "V0", "320").

    Returns:
        The output directory path string.
    """
    to_append: list[str] = []
    foldername = os.path.basename(path)

    if FLAC_FOLDER_RE.search(foldername):
        if LOSSLESS_FOLDER_RE.search(foldername):
            foldername = FLAC_FOLDER_RE.sub("MP3", foldername)
            foldername = LOSSLESS_FOLDER_RE.sub(bitrate, foldername)
        else:
            foldername = FLAC_FOLDER_RE.sub(f"MP3 {bitrate}", foldername)
    elif LOSSLESS_FOLDER_RE.search(foldername):
        foldername = LOSSLESS_FOLDER_RE.sub(bitrate, foldername)
        to_append.append("MP3")
    else:
        to_append.append(f"MP3 {bitrate}")

    if to_append:
        foldername += f" [{' '.join(to_append)}]"

    return os.path.join(os.path.dirname(path), foldername)


def _validate_lossless(path: str) -> None:
    """Validate that a folder contains only lossless audio files.

    Args:
        path: Path to the directory to validate.

    Raises:
        click.Abort: If a lossy file is found in the folder.
    """
    for _root, _, files in os.walk(path):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in {".mp3", ".m4a", ".ogg", ".opus"}:
                click.secho(f"A lossy file was found in the folder ({f}).", fg="red")
                raise click.Abort


def _get_id3_frame(tag_name: str, tag_value: list[str]) -> TXXX:
    """Convert a Vorbis comment tag to an ID3v2 frame.

    Args:
        tag_name: Vorbis comment tag name (lowercase).
        tag_value: List of tag values.

    Returns:
        An ID3v2 frame object.
    """
    if tag_name in VORBIS_TO_ID3_MAP:
        frame_name = VORBIS_TO_ID3_MAP[tag_name]
        frame_type = Frames[frame_name]
        return frame_type(encoding=3, text=tag_value)
    return TXXX(encoding=3, desc=tag_name, text=tag_value)


def _prepare_tags(tags: dict[str, list[str]]) -> dict[str, list[str]]:
    """Clean and normalize FLAC tags for ID3 conversion.

    Removes replaygain and encoder tags, merges track/disc totals.

    Args:
        tags: Raw tag dictionary from FLAC file.

    Returns:
        New cleaned tag dictionary.

    Raises:
        ValueError: If conflicting total values are found.
    """
    # Filter out unwanted tags
    result = {k: v for k, v in tags.items() if not k.startswith("replaygain") and k != "encoder"}

    # Merge track/disc totals into number tags
    for tag, tots in _TOT_MAP.items():
        if tag not in result:
            continue
        used = tots & result.keys()
        if not used:
            continue

        tot_vals: set[int] = set()
        invalid_entries: list[tuple[str, str]] = []
        for t in used:
            try:
                tot_vals.add(int(result[t][0]))
            except ValueError:
                invalid_entries.append((t, result[t][0]))
        if invalid_entries:
            details = ", ".join(f"{name}={value!r}" for name, value in invalid_entries)
            raise ValueError(f"Non-integer total values for {tag}: {details}")

        # Remove total keys
        result = {k: v for k, v in result.items() if k not in used}

        if len(tot_vals) == 1:
            total = str(tot_vals.pop())
            nr = result[tag][0]
            result = {**result, tag: [f"{nr}/{total}"]}
        else:
            raise ValueError(f"conflicting values of {' and '.join(used)}")

    return result


def _parse_flac_tags(flac_path: Path) -> tuple[flac.FLAC, dict[str, list[str]]]:
    """Read a FLAC file and extract its cleaned tags.

    Args:
        flac_path: Path to the FLAC file.

    Returns:
        Tuple of (FLAC object, cleaned tag dictionary).

    Raises:
        ValueError: If FLAC file has no tags or unexpected tag type.
    """
    fl = flac.FLAC(flac_path)
    tags = fl.tags
    if tags is None:
        raise ValueError(f"FLAC file has no tags: {flac_path}")
    if not isinstance(tags, VCFLACDict):
        raise ValueError(f"FLAC tags are not VCommentDict: {flac_path}")
    return fl, _prepare_tags(tags.as_dict())


def _collect_transcode_items(
    path: str,
    new_path: str,
) -> list[TranscodeItem]:
    """Collect all FLAC files and compute their output paths and tags.

    Args:
        path: Source album directory path.
        new_path: Destination album directory path.

    Returns:
        List of TranscodeItem structs.
    """
    src_path = Path(path)
    dst_path = Path(new_path)
    items: list[TranscodeItem] = []

    for flac_file in sorted(src_path.rglob("*.flac")):
        fl, tag_dict = _parse_flac_tags(flac_file)
        rel = flac_file.relative_to(src_path).with_suffix(".mp3")
        mp3_path = dst_path / rel
        items.append(TranscodeItem(src=str(flac_file), dst=str(mp3_path), flac_obj=fl, tags=tag_dict))

    return items


# ---------------------------------------------------------------------------
# Side-effect functions
# ---------------------------------------------------------------------------


def _copy_tags(tag_dict: dict[str, list[str]], flac_obj: flac.FLAC, mp3_path: Path) -> None:
    """Copy tags and embedded pictures from a FLAC object to an MP3 file.

    Args:
        tag_dict: Cleaned tag dictionary.
        flac_obj: Source FLAC file object.
        mp3_path: Path to the destination MP3 file.

    Raises:
        ValueError: If MP3 tags cannot be created.
    """
    mp3_thing = mp3.MP3(mp3_path)
    click.secho(f"     Copy tags: {mp3_path}", fg="cyan")

    if not mp3_thing.tags:
        mp3_thing.add_tags()
    if mp3_thing.tags is None:
        raise ValueError(f"Failed to create tags for MP3 file: {mp3_path}")

    for k, v in tag_dict.items():
        mp3_thing.tags.add(_get_id3_frame(k, v))

    for pic in flac_obj.pictures:
        mp3_thing.tags.add(APIC(encoding=3, mime=pic.mime, type=pic.type, desc=pic.desc, data=pic.data))

    mp3_thing.save(v1=0, v2_version=4)


def _copy_extra_files(path: str, new_path: str) -> None:
    """Copy non-audio files (images, text, etc.) to the output directory.

    Args:
        path: Source album directory path.
        new_path: Destination album directory path.
    """
    src_path = Path(path)
    dst_path = Path(new_path)

    for p in src_path.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in COPY_EXTENSIONS:
            continue
        rel = p.relative_to(src_path)
        click.secho(f"Copy {rel}", fg="cyan")
        out = dst_path / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(p, out)


async def _flac_to_mp3(lame_qual: Bitrate, flac_path: str, mp3_path: str) -> None:
    """Decode a FLAC file and pipe directly to LAME for MP3 encoding.

    Uses an OS-level pipe to connect flac stdout to lame stdin,
    avoiding temporary files and keeping data in kernel space.

    Args:
        lame_qual: LAME quality setting key (e.g. "V0", "320").
        flac_path: Path to the source FLAC file.
        mp3_path: Destination path for the MP3 file.

    Raises:
        RuntimeError: If FLAC decoding or LAME encoding fails.
    """
    Path(mp3_path).parent.mkdir(parents=True, exist_ok=True)

    flac_err = b""
    lame_err = b""
    flac_rc = 0
    lame_rc = 0

    read_fd, write_fd = os.pipe()
    try:
        async with await anyio.open_process(
            ["flac", "-Vdsc", "-o", "-", flac_path],
            stdout=write_fd,
            stderr=subprocess.PIPE,
        ) as flac_proc:
            os.close(write_fd)
            write_fd = -1

            async with await anyio.open_process(
                [
                    "lame",
                    *LAME_COMMAND_MAP[lame_qual],
                    "--quiet",
                    "--add-id3v2",
                    "--ignore-tag-errors",
                    "-",
                    mp3_path,
                ],
                stdin=read_fd,
                stderr=subprocess.PIPE,
            ) as lame_proc:
                os.close(read_fd)
                read_fd = -1

                await lame_proc.wait()
                if lame_proc.stderr:
                    lame_err = await lame_proc.stderr.receive()
                lame_rc = lame_proc.returncode or 0

            await flac_proc.wait()
            if flac_proc.stderr:
                flac_err = await flac_proc.stderr.receive()
            flac_rc = flac_proc.returncode or 0
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if read_fd >= 0:
            os.close(read_fd)

    if flac_rc:
        err = flac_err.decode()
        if err:
            click.secho(err, fg="yellow")
        raise RuntimeError(f"FLAC decoding failed with code {flac_rc}")

    if lame_rc:
        raise RuntimeError(lame_err.decode() or "LAME encoding failed")


async def _transcode_audio_files(
    items: list[TranscodeItem],
    bitrate: Bitrate,
) -> None:
    """Transcode FLAC files to MP3 concurrently.

    Args:
        items: List of TranscodeItem structs.
        bitrate: LAME quality setting key (e.g. "V0", "320").
    """
    if not items:
        return

    async def _transcode_one(file: str, idx: int) -> None:
        item = items[idx]
        if item.flac_obj.info.channels > 2:
            raise ValueError(f"{item.src} has {item.flac_obj.info.channels} channels. Cannot convert to MP3.")
        click.secho(f"Encoding: {item.dst}", fg="cyan")
        await _flac_to_mp3(bitrate, item.src, item.dst)
        _copy_tags(item.tags, item.flac_obj, Path(item.dst))

    file_paths = [item.src for item in items]
    await process_files(file_paths, _transcode_one, "Transcoding")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def transcode_folder(path: str, bitrate: Bitrate) -> str:
    """Transcode a lossless folder to MP3 at the specified bitrate.

    Args:
        path: Path to the directory containing lossless audio files.
        bitrate: Target MP3 bitrate (e.g. "V0", "320").

    Returns:
        Path to the newly created transcoded directory.
    """
    _validate_lossless(path)
    new_path = _build_output_path(path, bitrate)

    if os.path.isdir(new_path):
        expected_mp3s = {Path(item.dst).name for item in _collect_transcode_items(path, new_path)}
        existing_files = {f for f in os.listdir(new_path) if f.lower().endswith(".mp3")}
        if expected_mp3s and expected_mp3s <= existing_files:
            click.secho(f"{new_path} already exists.", fg="yellow")
            return new_path
        click.secho(
            f"{new_path} exists but appears incomplete, re-transcoding...",
            fg="yellow",
        )
        shutil.rmtree(new_path)

    items = _collect_transcode_items(path, new_path)
    _copy_extra_files(path, new_path)
    await _transcode_audio_files(items, bitrate)

    return new_path


def generate_transcode_description(url: str, bitrate: Bitrate) -> str:
    """Generate a BBCode description for a transcoded upload.

    Args:
        url: URL of the source torrent.
        bitrate: Target MP3 bitrate label ("V0" or "320").

    Returns:
        BBCode formatted description string.
    """
    lame_command = " ".join(LAME_COMMAND_MAP[bitrate])

    return (
        f"[b]Source:[/b] {url}\n"
        f"[b]Transcode process:[/b] "
        f"[code]flac -Vdsc -- input.flac | lame -S {lame_command} --ignore-tag-errors - output.mp3[/code]\n"
    )
