import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import anyio
import asyncclick as click
import msgspec

from salmon import cfg
from salmon.common.constants import IMAGE_EXTENSIONS, LOSSY_EXTENSIONS
from salmon.common.files import get_audio_files, process_files
from salmon.converter.conversions import (
    changed_sources,
    changed_tracks,
    conversion_of,
    missing_files,
    record_conversion,
    recorded_files,
    source_fingerprints,
    source_of,
)
from salmon.errors import InvalidSampleRate, UploadError
from salmon.release_notification import get_version
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.foldername import (
    drop_resolution_token,
    holds_resolution_token,
    resolution_token,
    swap_resolution_token,
)

BitDepth = Literal[16, 24]

SOX_DEPTH_ARGS: dict[BitDepth, list[str]] = {
    16: ["-R", "-G", "-b", "16"],
    24: ["-R", "-G"],
}


class ConvertItem(msgspec.Struct, frozen=True):
    """A file that needs sample rate / bit depth conversion."""

    src: str
    dst: str
    sample_rate: int
    target_rate: int


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _resolve_sample_rate(sample_rate: int) -> int:
    """Determine the standard sample rate family for a given rate.

    Args:
        sample_rate: The original sample rate.

    Returns:
        44100 or 48000 depending on the rate family.

    Raises:
        InvalidSampleRate: If the rate doesn't belong to either family.
    """
    if sample_rate % 44100 == 0:
        return 44100
    if sample_rate % 48000 == 0:
        return 48000
    raise InvalidSampleRate


def _build_output_path(path: str, bit_depth: BitDepth, sample_rate: int | None, output_dir: str | None = None) -> str:
    """Generate the output directory path based on source path and conversion params.

    Args:
        path: Source album directory path.
        bit_depth: Target bit depth.
        sample_rate: Target sample rate, or None.
        output_dir: Directory the output goes into; the source's own directory by default.

    Returns:
        The output directory path string.
    """
    foldername = os.path.basename(path)
    # A stale resolution token, e.g. "24-192", is computed from the source's actual files, not
    # guessed at with a pattern, so it cannot also eat unrelated digits from the album title. It
    # only counts if that exact text is actually in the name; a folder that happens to be 24/96
    # inside is not the same thing as one whose name says so.
    audio_info = gather_audio_info(path)
    current_token = resolution_token(audio_info)
    has_token = holds_resolution_token(foldername, current_token)
    # A {resolution}-only name (the recommended template does not pair it with {format}) has no
    # "24bit"/"16bit FLAC" wording to rewrite: swap its token for the conversion's own one in
    # place instead of running it through the FLAC/bit-depth rewriting below.
    carries_bit_depth_wording = bool(re.search(r"\d+ ?bit FLAC", foldername, flags=re.IGNORECASE))

    if has_token and not carries_bit_depth_wording:
        target_rate = sample_rate
        if target_rate is None:
            (source_rate,) = {info["sample rate"] for info in audio_info.values()}
            target_rate = _resolve_sample_rate(source_rate)
        new_token = resolution_token({"_": {"precision": bit_depth, "sample rate": target_rate}})
        if new_token:
            foldername = swap_resolution_token(foldername, current_token, new_token)
        else:
            foldername = drop_resolution_token(foldername, current_token)
        return os.path.join(output_dir or os.path.dirname(path), foldername)

    if has_token:
        # A template pairing {format} and {resolution} (e.g. "24bit FLAC 24-96"): drop the stale
        # token so the FLAC/bit-depth rewriting below does not have to work around it.
        foldername = drop_resolution_token(foldername, current_token)

    if re.search(r"24 ?bit FLAC", foldername, flags=re.IGNORECASE):
        foldername = re.sub(r"24 ?bit FLAC", "FLAC", foldername, flags=re.IGNORECASE)
    elif re.search("FLAC", foldername, flags=re.IGNORECASE):
        foldername = re.sub("FLAC", "16bit FLAC", foldername, flags=re.IGNORECASE)
    else:
        foldername += " [FLAC]"

    if sample_rate and bit_depth == 24:
        foldername = re.sub(
            "FLAC",
            f"24-{sample_rate / 1000:.0f}",
            foldername,
            flags=re.IGNORECASE,
        )

    return os.path.join(output_dir or os.path.dirname(path), foldername)


def _collect_convert_items(
    path: str,
    new_path: str,
    sample_rate: int | None,
) -> list[ConvertItem]:
    """Collect all 24-bit FLAC files and compute their output paths and target rates.

    Args:
        path: Source album directory path.
        new_path: Destination album directory path.
        sample_rate: Explicit target sample rate, or None for automatic.

    Returns:
        List of ConvertItem structs.
    """
    src_path = Path(path)
    dst_path = Path(new_path)
    audio_info = gather_audio_info(path)

    items: list[ConvertItem] = []
    for info_file, file_info in audio_info.items():
        if file_info["precision"] != 24:
            continue
        src_file = src_path / info_file
        rel = Path(info_file)
        dst_file = dst_path / rel
        target_rate = sample_rate if sample_rate else _resolve_sample_rate(file_info["sample rate"])
        items.append(
            ConvertItem(
                src=str(src_file),
                dst=str(dst_file),
                sample_rate=file_info["sample rate"],
                target_rate=target_rate,
            )
        )

    return items


# ---------------------------------------------------------------------------
# Side-effect functions
# ---------------------------------------------------------------------------


def _validate_lossless(path: str) -> None:
    """Validate that a folder contains no lossy audio files.

    Args:
        path: Path to the directory to validate.

    Raises:
        click.Abort: If a lossy file is found.
    """
    for _root, _, files in os.walk(path):
        for f in files:
            if os.path.splitext(f)[1].lower() in LOSSY_EXTENSIONS:
                click.secho(f"A lossy file was found in the folder ({f}).", fg="red")
                raise click.Abort


def _copy_extra_files(
    path: str,
    new_path: str,
    convert_srcs: frozenset[str],
    *,
    essential_only: bool = False,
) -> None:
    """Copy non-conversion files (images, text, 16-bit audio) to the output directory.

    Args:
        path: Source album directory path.
        new_path: Destination album directory path.
        convert_srcs: Set of source paths that will be converted (to exclude).
        essential_only: If True, only copy image files; skip everything else.
    """
    src_path = Path(path)
    dst_path = Path(new_path)

    for p in src_path.rglob("*"):
        if not p.is_file() or str(p) in convert_srcs:
            continue
        rel = p.relative_to(src_path)
        if essential_only and p.suffix.lower() not in IMAGE_EXTENSIONS:
            click.secho(f"Skip  {rel}", fg="yellow")
            continue
        out = dst_path / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        click.secho(f"Copy  {rel}", fg="cyan")
        shutil.copy(p, out)


async def _convert_audio_files(
    items: list[ConvertItem],
    bit_depth: BitDepth,
) -> None:
    """Convert audio files concurrently using sox.

    Args:
        items: List of ConvertItem structs.
        bit_depth: Target bit depth (16 or 24).
    """
    if not items:
        return

    async def _convert_one(file: str, idx: int) -> None:
        item = items[idx]
        Path(item.dst).parent.mkdir(parents=True, exist_ok=True)

        command = [
            "sox",
            item.src,
            *SOX_DEPTH_ARGS[bit_depth],
            item.dst,
            "rate",
            "-v",
            "-L",
            str(item.target_rate),
            "dither",
        ]

        result = await anyio.run_process(command, check=False)
        if result.returncode != 0:
            err = result.stderr.decode() if result.stderr else ""
            if err:
                click.secho(err, fg="yellow")
            raise RuntimeError(f"sox conversion failed for {os.path.basename(item.src)} with code {result.returncode}")

    file_paths = [item.src for item in items]
    await process_files(file_paths, _convert_one, "Converting")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _recorded_rate(items: list[ConvertItem]) -> int | list[int]:
    """The target sample rate as a conversion record holds it: the one rate, or each rate in order."""
    rates = sorted({item.target_rate for item in items})
    return rates[0] if len(rates) == 1 else rates


def _check_finished_conversion(path: str, new_path: str, items: list[ConvertItem], bit_depth: BitDepth) -> None:
    """Make sure new_path holds every converted file and a record that it is this conversion of path.

    The record is written after the last file, so a run that died has none. It lists the files the conversion
    wrote, under the names the folder check may have shortened since; a record from before it did is checked
    against the names this conversion would give them.

    Raises:
        UploadError: new_path is not this conversion, complete.
    """
    record = conversion_of(new_path)
    # The name alone can match: a 16-bit conversion at 44.1 kHz and at 48 kHz can share one.
    wanted = {"kind": "downconvert", "bit_depth": bit_depth, "sample_rate": _recorded_rate(items)}
    recorded = None if record is None else recorded_files(record)
    if (
        record is not None
        and os.path.realpath(record["source"]) == os.path.realpath(source_of(path))
        and all(record.get(key) == value for key, value in wanted.items())
        # A track the album gained or lost since is not in this conversion, or no longer in the album.
        and (recorded is None or _records_these_files(path, new_path, items, record, recorded))
    ):
        # Its files carry the tags and pictures the tracks had then: a retag since would go up without them.
        if changed := changed_sources(path, record):
            raise UploadError(
                f"{new_path} is salmon's conversion of this album at these settings, from before "
                f"{changed_tracks(changed)}: remove it and run again."
            )
        missing = missing_files(new_path, record)
        if missing:
            raise UploadError(
                f"{new_path} is salmon's conversion of this album at these settings, but {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} missing: remove it and run again."
            )
        if missing is not None or all(os.path.isfile(item.dst) for item in items):
            return
    raise UploadError(
        f"{new_path} is incomplete or not salmon's conversion of this album at these settings: remove it and run again."
    )


def _records_these_files(
    path: str, new_path: str, items: list[ConvertItem], record: dict[str, Any], recorded: dict[str, str]
) -> bool:
    """Whether a record lists the album's files as they are now: each one to convert, and none since gone."""
    album_files = record.get("album_files")
    if isinstance(album_files, list):
        return sorted(album_files) == sorted(get_audio_files(path))
    # A record from before records listed the album's files: --essential-only leaves 16-bit ones out of it.
    converted = {os.path.relpath(item.dst, new_path) for item in items}
    return converted <= recorded.keys() and all(os.path.isfile(os.path.join(path, source)) for source in recorded)


async def convert_folder(
    path: str,
    bit_depth: BitDepth = 16,
    sample_rate: int | None = None,
    essential_only: bool = False,
    output_dir: str | None = None,
) -> tuple[int | None, str]:
    """Convert a folder of 24-bit FLAC files to the target bit depth.

    Args:
        path: Path to the source album directory.
        bit_depth: Target bit depth. Defaults to 16.
        sample_rate: Target sample rate. None for automatic detection.
        essential_only: If True, only image files are copied; all other extra
            files (scans, cues, logs, etc.) are skipped.
        output_dir: Directory the output goes into; the source's own directory by default.

    Returns:
        Tuple of (final_sample_rate, new_folder_path).
    """
    _validate_lossless(path)
    new_path = _build_output_path(path, bit_depth, sample_rate, output_dir)
    # Resolved, so a symlink on the way cannot carry the output into a library.
    if cfg.directory.protects(new_path):
        raise UploadError(f"Not converting into {new_path}: it is in library_dirs, or holds one.")

    items = _collect_convert_items(path, new_path, sample_rate)
    if os.path.isdir(new_path):
        # Never removed: unlike a transcode's, this name can belong to a folder the user made.
        # With nothing left to convert, a record still tells an album that lost its last 24-bit track.
        if items or conversion_of(new_path) is not None:
            _check_finished_conversion(path, new_path, items, bit_depth)
        click.secho(f"{new_path} already exists.", fg="yellow")
        return sample_rate, new_path

    convert_srcs = frozenset(item.src for item in items)
    _copy_extra_files(path, new_path, convert_srcs, essential_only=essential_only)
    await _convert_audio_files(items, bit_depth)

    final_rate = items[-1].target_rate if items else None
    if items:
        # Each audio file keeps its name: the converted ones, and those copied as they are.
        written = [os.path.relpath(item.dst, new_path) for item in items] + get_audio_files(new_path)
        files = {name: name for name in sorted(set(written))}
        record_conversion(
            new_path,
            source=source_of(path),
            kind="downconvert",
            bit_depth=bit_depth,
            sample_rate=_recorded_rate(items),
            files=files,
            # Every audio file of the album, the 16-bit ones copied as they are too: to tell when it gains or loses one.
            album_files=sorted(get_audio_files(path)),
            source_fingerprints=source_fingerprints(path, get_audio_files(path)),
        )
    return final_rate or sample_rate, new_path


def conversion_note(url: str, sample_rate: int | Sequence[int] | None, bit_depth: BitDepth = 16) -> str:
    """The source and the sox commands of a conversion, without the specifics line or the footer."""
    if not sample_rate:
        return ""
    rates = [sample_rate] if isinstance(sample_rate, int) else list(sample_rate)
    depth_args = " ".join(SOX_DEPTH_ARGS[bit_depth])
    sox_cmds = "\n".join(f"sox input.flac {depth_args} output.flac rate -v -L {rate} dither" for rate in rates)
    return f"[b]Source:[/b] {url}\n[b]Transcode process:[/b] [code]{sox_cmds}[/code]\n"


def generate_conversion_description(url: str, sample_rate: int | Sequence[int] | None, bit_depth: BitDepth = 16) -> str:
    """Generate a BBCode description for the conversion process.

    Args:
        url: Source URL for attribution.
        sample_rate: The sample rate used in conversion; several when a mixed-family folder kept each track's family.
        bit_depth: Target bit depth (16 or 24).

    Returns:
        Formatted description string.
    """
    if not sample_rate:
        return ""
    rates = [sample_rate] if isinstance(sample_rate, int) else list(sample_rate)
    specifics = " / ".join(f"{rate / 1000:.01f}" for rate in rates)
    return (
        f"Encode Specifics: {bit_depth} bit {specifics} kHz\n"
        f"{conversion_note(url, sample_rate, bit_depth)}"
        f"[hr]Uploaded with [url=https://github.com/smokin-salmon/smoked-salmon]"
        f"[b]smoked-salmon[/b] v{get_version()}[/url]"
    )
