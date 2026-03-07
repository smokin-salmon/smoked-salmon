import os
import re
import shutil
from pathlib import Path
from typing import Literal

import anyio
import asyncclick as click
import msgspec

from salmon.common.files import process_files
from salmon.errors import InvalidSampleRate
from salmon.release_notification import get_version
from salmon.tagger.audio_info import gather_audio_info

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


def _build_output_path(path: str, bit_depth: BitDepth) -> str:
    """Generate the output directory path based on source path and conversion params.

    Args:
        path: Source album directory path.
        bit_depth: Target bit depth.

    Returns:
        The output directory path string.
    """
    foldername = os.path.basename(path)
    if re.search(r"24 ?bit FLAC", foldername, flags=re.IGNORECASE):
        foldername = re.sub(r"24 ?bit FLAC", "FLAC", foldername, flags=re.IGNORECASE)
    elif re.search("FLAC", foldername, flags=re.IGNORECASE):
        foldername = re.sub("FLAC", "16bit FLAC", foldername, flags=re.IGNORECASE)
    else:
        foldername += " [FLAC]"

    return os.path.join(os.path.dirname(path), foldername)


def _collect_convert_items(
    path: str,
    new_path: str,
) -> list[ConvertItem]:
    """Collect all 24-bit FLAC files that need downconversion.

    Skips files that are already at or below their target sample rate
    (never upconverts). Each file's target rate is determined individually
    by its sample rate family.

    Args:
        path: Source album directory path.
        new_path: Destination album directory path.

    Returns:
        List of ConvertItem structs for files that need conversion.
    """
    src_path = Path(path)
    dst_path = Path(new_path)
    audio_info = gather_audio_info(path)

    items: list[ConvertItem] = []
    for info_file, file_info in audio_info.items():
        if file_info["precision"] != 24:
            continue
        target_rate = _resolve_sample_rate(file_info["sample rate"])
        if target_rate >= file_info["sample rate"]:
            continue  # Never upconvert
        src_file = src_path / info_file
        rel = Path(info_file)
        dst_file = dst_path / rel
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


def _copy_extra_files(path: str, new_path: str, convert_srcs: frozenset[str]) -> None:
    """Copy non-conversion files (images, text, 16-bit audio) to the output directory.

    Args:
        path: Source album directory path.
        new_path: Destination album directory path.
        convert_srcs: Set of source paths that will be converted (to exclude).
    """
    src_path = Path(path)
    dst_path = Path(new_path)

    for p in src_path.rglob("*"):
        if not p.is_file() or str(p) in convert_srcs:
            continue
        rel = p.relative_to(src_path)
        out = dst_path / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        click.secho(f"Copy {rel}", fg="cyan")
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


async def convert_folder(
    path: str,
    bit_depth: BitDepth = 16,
) -> str:
    """Convert a folder of 24-bit FLAC files to the target bit depth.

    Each file's target sample rate is determined individually. Files already
    at or below their target rate are copied as-is (never upconverted).

    Args:
        path: Path to the source album directory.
        bit_depth: Target bit depth. Defaults to 16.

    Returns:
        Path to the output folder.
    """
    new_path = _build_output_path(path, bit_depth)

    if os.path.isdir(new_path):
        click.secho(f"{new_path} already exists.", fg="yellow")
        return new_path

    items = _collect_convert_items(path, new_path)
    convert_srcs = frozenset(item.src for item in items)
    _copy_extra_files(path, new_path, convert_srcs)
    await _convert_audio_files(items, bit_depth)

    return new_path


def generate_conversion_description(url: str, bit_depth: BitDepth = 16) -> str:
    """Generate a BBCode description for the conversion process.

    Args:
        url: Source URL for attribution.
        bit_depth: Target bit depth (16 or 24).

    Returns:
        Formatted description string.
    """
    depth_args = " ".join(SOX_DEPTH_ARGS[bit_depth])
    sox_cmd = f"sox input.flac {depth_args} output.flac rate -v -L <original_family_rate> dither"
    return (
        f"[b]Source:[/b] {url}\n"
        f"[b]Transcode process:[/b] [code]{sox_cmd}[/code]\n"
        f"[hr]Uploaded with [url=https://github.com/smokin-salmon/smoked-salmon]"
        f"[b]smoked-salmon[/b] v{get_version()}[/url]"
    )
