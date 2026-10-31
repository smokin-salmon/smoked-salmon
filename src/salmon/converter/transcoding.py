import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal

import asyncclick as click
import msgspec
from mutagen import flac, mp3
from mutagen.flac import VCFLACDict
from mutagen.id3 import APIC, ID3, TXXX, Frames

from salmon import cfg, interaction
from salmon.common.constants import IMAGE_EXTENSIONS, LOSSY_EXTENSIONS
from salmon.common.files import process_files
from salmon.converter.conversions import conversion_of, missing_files, record_conversion, recorded_files, source_of
from salmon.errors import UploadError
from salmon.release_notification import get_version
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.cover import MAX_PICTURES_AND_PADDING
from salmon.tagger.foldername import drop_resolution_token, resolution_token

Bitrate = Literal["V0", "320"]

# LAME encoding presets
LAME_COMMAND_MAP: dict[Bitrate, list[str]] = {
    "V0": ["-V", "0", "--vbr-new"],
    "320": ["-h", "-b", "320"],
}

SKIP_EXTENSIONS = frozenset((".cue", ".log", ".m3u", ".m3u8", ".accurip"))
FLAC_FOLDER_RE = re.compile(r"(24 ?bit )?FLAC", flags=re.IGNORECASE)
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


def _build_output_path(path: str, bitrate: Bitrate, output_dir: str | None = None) -> str:
    """Generate the output directory path for a transcoded release.

    Args:
        path: Source album directory path.
        bitrate: Target MP3 bitrate label (e.g. "V0", "320").
        output_dir: Directory the output goes into; the source's own directory by default.

    Returns:
        The output directory path string.
    """
    to_append: list[str] = []
    foldername = os.path.basename(path)
    # Drop a stale resolution token before deriving the new name: an MP3 transcode must not keep
    # claiming a FLAC resolution such as "24-96". The token is computed from the source's actual
    # files, not guessed at with a pattern, so it cannot also eat unrelated digits from the title.
    foldername = drop_resolution_token(foldername, resolution_token(gather_audio_info(path)))

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

    return os.path.join(output_dir or os.path.dirname(path), foldername)


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
            if ext in LOSSY_EXTENSIONS:
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
    if tag_name == "artists":
        # The name Picard writes; the FLAC's field names come lowercase, and every other one stays so.
        return TXXX(encoding=3, desc="ARTISTS", text=tag_value)
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

    # Any case, as _copy_extra_files skips them: a .FLAC track would otherwise be neither transcoded nor copied.
    flac_files = (p for p in src_path.rglob("*") if p.is_file() and p.suffix.lower() == ".flac")
    for flac_file in sorted(flac_files):
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

    if not mp3_thing.tags:
        mp3_thing.add_tags()
    if mp3_thing.tags is None:
        raise ValueError(f"Failed to create tags for MP3 file: {mp3_path}")

    for k, v in tag_dict.items():
        mp3_thing.tags.add(_get_id3_frame(k, v))

    # The FLAC's limit on pictures and padding holds the whole saved tag, text and padding included: a picture
    # stays only if the tag saved with it still fits.
    for pic in flac_obj.pictures:
        frame = APIC(encoding=3, mime=pic.mime, type=pic.type, desc=pic.desc, data=pic.data)
        # add() replaces a picture with the same desc, as it always did: that one goes back if this one does not fit.
        replaced = mp3_thing.tags.get(frame.HashKey)
        mp3_thing.tags.add(frame)
        mp3_thing.save(v1=0, v2_version=4)
        if ID3(mp3_path).size > MAX_PICTURES_AND_PADDING:
            if replaced is None:
                del mp3_thing.tags[frame.HashKey]
            else:
                mp3_thing.tags.add(replaced)
            # With no padding: a later save would keep the space the picture took as padding, over the limit.
            mp3_thing.save(v1=0, v2_version=4, padding=lambda _info: 0)

    mp3_thing.save(v1=0, v2_version=4)


def _copy_extra_files(path: str, new_path: str, *, essential_only: bool = False) -> None:
    """Copy non-audio files to the output directory.

    By default all non-FLAC files are copied except those in SKIP_EXTENSIONS.
    When essential_only is True, only image files (matching IMAGE_EXTENSIONS) are kept.

    Args:
        path: Source album directory path.
        new_path: Destination album directory path.
        essential_only: If True, only copy image files; skip everything else.
    """
    src_path = Path(path)
    dst_path = Path(new_path)

    for p in src_path.rglob("*"):
        if not p.is_file() or p.suffix.lower() == ".flac":
            continue
        rel = p.relative_to(src_path)
        if p.suffix.lower() in SKIP_EXTENSIONS:
            click.secho(f"Skip  {rel}", fg="yellow")
            continue
        if essential_only and p.suffix.lower() not in IMAGE_EXTENSIONS:
            click.secho(f"Skip  {rel}", fg="yellow")
            continue
        click.secho(f"Copy  {rel}", fg="cyan")
        out = dst_path / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(p, out)


async def _flac_to_mp3(lame_qual: Bitrate, flac_path: str, mp3_path: str) -> None:
    """Decode a FLAC file and pipe directly to LAME for MP3 encoding.

    Uses asyncio subprocesses to connect flac stdout to lame stdin,
    avoiding temporary files.

    Args:
        lame_qual: LAME quality setting key (e.g. "V0", "320").
        flac_path: Path to the source FLAC file.
        mp3_path: Destination path for the MP3 file.

    Raises:
        RuntimeError: If FLAC decoding or LAME encoding fails.
    """
    Path(mp3_path).parent.mkdir(parents=True, exist_ok=True)

    read_fd, write_fd = os.pipe()
    try:
        flac_proc = await asyncio.create_subprocess_exec(
            "flac",
            "-Vdsc",
            "-o",
            "-",
            flac_path,
            stdout=write_fd,
            stderr=asyncio.subprocess.PIPE,
        )
        os.close(write_fd)
        write_fd = -1

        try:
            lame_proc = await asyncio.create_subprocess_exec(
                "lame",
                *LAME_COMMAND_MAP[lame_qual],
                "--quiet",
                "--add-id3v2",
                "--ignore-tag-errors",
                "-",
                mp3_path,
                stdin=read_fd,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            flac_proc.kill()
            await flac_proc.wait()
            raise
        os.close(read_fd)
        read_fd = -1
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if read_fd >= 0:
            os.close(read_fd)

    try:
        _, flac_err = await flac_proc.communicate()
        _, lame_err = await lame_proc.communicate()
    except BaseException:
        # Cancelled (Ctrl-C, a salmon web job): the encoders must not go on writing the MP3 after the run.
        for proc in (flac_proc, lame_proc):
            if proc.returncode is None:
                proc.kill()
        await asyncio.gather(flac_proc.wait(), lame_proc.wait(), return_exceptions=True)
        raise

    if flac_proc.returncode:
        err = flac_err.decode()
        if err:
            click.secho(err, fg="yellow")
        raise RuntimeError(f"FLAC decoding failed with code {flac_proc.returncode}")

    if lame_proc.returncode:
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
        await _flac_to_mp3(bitrate, item.src, item.dst)
        _copy_tags(item.tags, item.flac_obj, Path(item.dst))

    file_paths = [item.src for item in items]
    await process_files(file_paths, _transcode_one, "Transcoding")


def _validate_channel_count(items: list[TranscodeItem]) -> None:
    """Refuse a source of more than two channels, before anything is written or a prior output deleted."""
    for item in items:
        if item.flac_obj.info.channels > 2:
            raise ValueError(f"{item.src} has {item.flac_obj.info.channels} channels. Cannot convert to MP3.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def transcode_folder(
    path: str, bitrate: Bitrate, essential_only: bool = False, output_dir: str | None = None
) -> str:
    """Transcode a lossless folder to MP3 at the specified bitrate.

    Args:
        path: Path to the directory containing lossless audio files.
        bitrate: Target MP3 bitrate (e.g. "V0", "320").
        essential_only: If True, only image files are copied; all other extra
            files (scans, cues, logs, etc.) are skipped.
        output_dir: Directory the output goes into; the source's own directory by default.

    Returns:
        Path to the newly created transcoded directory.
    """
    _validate_lossless(path)
    new_path = _build_output_path(path, bitrate, output_dir)
    # Resolved, so a symlink on the way cannot carry the output, or the removal of an incomplete one, into a library.
    if cfg.directory.protects(new_path):
        raise UploadError(f"Not transcoding into {new_path}: it is in library_dirs, or holds one.")

    # Before the overwrite below: a source that cannot be transcoded must not cost the output already there.
    items = _collect_transcode_items(path, new_path)
    _validate_channel_count(items)

    if os.path.isdir(new_path):
        if _is_finished_transcode(path, new_path, bitrate):
            click.secho(f"{new_path} already exists.", fg="yellow")
            return new_path
        if not items:
            raise UploadError(f"Refusing to overwrite {new_path}: no expected MP3 output was computed.")
        # Not salmon's transcode of this album as far as it can tell, such as an upload made with another file
        # template, which may be seeding: deleted only when the user says so. -yyy takes the default, no.
        if await interaction.assume_defaults():
            raise UploadError(
                f"Not overwriting {new_path} with -yyy (upload.yes_all): it doesn't match the expected transcode, "
                "and you may be seeding it. Run without -yyy, and with upload.yes_all off, to choose."
            )
        if not await interaction.confirm(
            click.style(
                f"{new_path} exists but doesn't match the expected transcode. Delete it and re-transcode?",
                fg="magenta",
                bold=True,
            ),
            default=False,
        ):
            raise UploadError(f"Not overwriting existing folder {new_path}.")
        click.secho(
            f"{new_path} exists but appears incomplete, re-transcoding...",
            fg="yellow",
        )
        shutil.rmtree(new_path)

    _copy_extra_files(path, new_path, essential_only=essential_only)
    await _transcode_audio_files(items, bitrate)

    if items:
        files = {os.path.relpath(item.src, path): os.path.relpath(item.dst, new_path) for item in items}
        record_conversion(new_path, source=source_of(path), kind="transcode", bitrate=bitrate, files=files)
    return new_path


def _is_finished_transcode(path: str, new_path: str, bitrate: Bitrate) -> bool:
    """Whether new_path, which exists, is this transcode of path, complete; False when it is to be made again.

    salmon's record of the transcode lists the files it wrote, under the names the folder check may have shortened
    since. Only a folder with no record, a transcode that stopped before its last file, is ever made again: one with a
    record may be what a torrent seeds from. A record of another conversion (another kind, bitrate or source, or this
    album before it gained or lost a track) is refused whatever names the folder holds.

    Raises:
        UploadError: new_path has a record and is not this transcode, complete.
    """
    record = conversion_of(new_path)
    items = _collect_transcode_items(path, new_path)
    if record is not None:
        if differs := _how_record_differs(record, path, bitrate, {os.path.relpath(item.src, path) for item in items}):
            raise UploadError(f"{new_path} holds {differs}, not this transcode: remove it and run again.")
        missing = missing_files(new_path, record)
        if missing:
            raise UploadError(
                f"{new_path} is salmon's transcode of this album at {bitrate}, but {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} missing: remove it and run again."
            )
        if missing is not None:
            return True
    # No record, or this transcode's from before salmon listed the files it wrote: the paths it would give them.
    expected_mp3s = {Path(item.dst).relative_to(new_path) for item in items}
    existing_files = {p.relative_to(new_path) for p in Path(new_path).rglob("*") if p.suffix.lower() == ".mp3"}
    if expected_mp3s and expected_mp3s <= existing_files:
        return True
    if record is not None:
        raise UploadError(
            f"{new_path} is incomplete or not salmon's transcode of this album at {bitrate}: remove it and run again."
        )
    return False


def _how_record_differs(record: dict[str, Any], path: str, bitrate: Bitrate, sources: set[str]) -> str | None:
    """What a conversion record is of, when it is not this transcode of path at bitrate; None when it is.

    ``sources`` are the files of path the transcode makes, relative to it.
    """
    if os.path.realpath(record["source"]) != os.path.realpath(source_of(path)):
        return f"a conversion of another album ({record['source']})"
    if record["kind"] != "transcode":
        return "a downconversion of this album"
    if record["bitrate"] != bitrate:
        return f"a transcode of this album at {record['bitrate']}"
    # A track the album gained or lost since is not in this transcode, or no longer in the album.
    recorded = recorded_files(record)
    if recorded is not None and recorded.keys() != sources:
        return "a transcode of this album from before its files changed"
    return None


def transcode_note(url: str, bitrate: Bitrate) -> str:
    """The source and the lame command of a transcode, without the footer."""
    lame_command = " ".join(LAME_COMMAND_MAP[bitrate])
    return (
        f"[b]Source:[/b] {url}\n"
        f"[b]Transcode process:[/b] "
        f"[code]flac -Vdsc -- input.flac | lame -S {lame_command} --ignore-tag-errors - output.mp3[/code]\n"
    )


def generate_transcode_description(url: str, bitrate: Bitrate) -> str:
    """Generate a BBCode description for a transcoded upload.

    Args:
        url: URL of the source torrent.
        bitrate: Target MP3 bitrate label ("V0" or "320").

    Returns:
        BBCode formatted description string.
    """
    return (
        f"{transcode_note(url, bitrate)}"
        f"[hr]Uploaded with [url=https://github.com/smokin-salmon/smoked-salmon]"
        f"[b]smoked-salmon[/b] v{get_version()}[/url]"
    )
