import os
import zlib
from collections.abc import Generator, Iterable
from typing import Any

import anyio
import anyio.to_thread
import asyncclick as click
import av
import cambia

from salmon.common.files import process_files
from salmon.errors import CRCMismatchError, EditedLogError


def _get_audio_duration_sectors(filepath: str) -> int:
    """Get the duration of an audio file in CD sectors using PyAV.

    Uses duration_ts to calculate precise sector count:
    duration_ts * 75 / 44100 = sectors (since 44100 samples/sec ÷ 75 sectors/sec = 588 samples/sector)

    Args:
        filepath: Path to the audio file.

    Returns:
        Duration in CD sectors.

    Raises:
        RuntimeError: If there's an error getting duration from the file.
    """
    try:
        with av.open(filepath) as container:
            audio_stream = container.streams.audio[0]
            duration_ts = audio_stream.duration
            if duration_ts is None:
                raise RuntimeError(f"Cannot determine duration_ts for {filepath}")
            # time_base converts duration_ts to seconds; then multiply by 75 for sectors
            time_base = audio_stream.time_base
            if time_base is None:
                raise RuntimeError(f"Cannot determine time_base for {filepath}")
            duration_seconds = float(duration_ts * time_base)
            return int(duration_seconds * 75)
    except (av.FFmpegError, IndexError) as e:
        raise RuntimeError(f"Error getting duration in sectors for {filepath}: {e}") from e


def _iter_pcm_chunks(filepath: str) -> Generator[bytes, None, None]:
    """Yield raw s16le PCM chunks from an audio file.

    Decodes audio frame-by-frame and yields each resampled chunk,
    keeping memory usage constant regardless of file size.

    Args:
        filepath: Path to the audio file.

    Yields:
        Raw PCM bytes per decoded frame in s16le stereo 44100Hz format.

    Raises:
        RuntimeError: If decoding fails.
    """
    try:
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)
        with av.open(filepath) as container:
            for frame in container.decode(audio=0):
                for resampled_frame in resampler.resample(frame):
                    yield resampled_frame.to_ndarray().tobytes()
            # Flush the resampler
            for resampled_frame in resampler.resample(None):
                yield resampled_frame.to_ndarray().tobytes()
    except av.FFmpegError as e:
        raise RuntimeError(f"Error decoding audio from {filepath}: {e}") from e


def _crc32_from_chunks(chunks: Iterable[bytes]) -> str:
    """Calculate CRC32 incrementally from an iterable of byte chunks.

    Args:
        chunks: Iterable of raw byte chunks.

    Returns:
        CRC32 hash as uppercase 8-char hex string.
    """
    crc = 0
    for chunk in chunks:
        crc = zlib.crc32(chunk, crc)
    return format(crc & 0xFFFFFFFF, "08X")


async def _calculate_file_crc_async(filepath: str, _: Any = None) -> str:
    """Calculate CRC32 hash for a single audio file in a thread.

    Streams audio frame-by-frame to avoid loading the entire file into memory.

    Args:
        filepath: Path to the audio file.
        _: Unused parameter for compatibility with process_files interface.

    Returns:
        CRC32 hash as uppercase hex string.
    """
    return await anyio.to_thread.run_sync(lambda: _crc32_from_chunks(_iter_pcm_chunks(filepath)))


def _iter_range_pcm_chunks(track_files: list[str], toc_entries: list[cambia.TocEntry]) -> Generator[bytes, None, None]:
    """Yield PCM chunks for a range rip reconstruction, including silence gaps.

    Streams audio frame-by-frame per track, inserting silence where TOC
    indicates gaps, keeping memory usage constant.

    Args:
        track_files: List of paths to track audio files.
        toc_entries: List of TOC entries from the log file.

    Yields:
        Raw PCM bytes per decoded frame or silence chunk.

    Raises:
        ValueError: If track file count doesn't match TOC entry count.
        RuntimeError: If decoding fails.
    """
    # Sort track files by track number to match TOC entries order
    sorted_files = sorted(track_files, key=lambda f: os.path.basename(f))

    if len(sorted_files) != len(toc_entries):
        raise ValueError(f"Mismatch: {len(sorted_files)} track files but {len(toc_entries)} TOC entries")

    for track_file, toc_entry in zip(sorted_files, toc_entries, strict=True):
        track_length_sectors = toc_entry.end_sector - toc_entry.start_sector + 1
        actual_duration_sectors = _get_audio_duration_sectors(track_file)

        # Yield decoded PCM chunks from the track
        yield from _iter_pcm_chunks(track_file)

        # If there's a gap after this track, yield silence
        if track_length_sectors > actual_duration_sectors:
            gap_sectors = track_length_sectors - actual_duration_sectors
            gap_samples = int(gap_sectors * 44100 / 75)

            if gap_sectors > 0:
                click.secho(
                    f"Adding {gap_sectors / 75:.2f}s silence gap ({gap_sectors} sectors, {gap_samples} samples)",
                    fg="yellow",
                )
                # s16le stereo = 4 bytes per sample
                yield b"\x00" * (gap_samples * 4)


async def _calculate_range_crc_async(track_files: list[str], toc_entries: list[cambia.TocEntry]) -> str:
    """Calculate CRC32 hash for a range rip reconstruction in a thread.

    Args:
        track_files: List of paths to track audio files.
        toc_entries: List of TOC entries from the log file.

    Returns:
        CRC32 hash as uppercase hex string.
    """
    return await anyio.to_thread.run_sync(lambda: _crc32_from_chunks(_iter_range_pcm_chunks(track_files, toc_entries)))


async def check_log_cambia(logpath: str, basepath: str) -> None:
    """Check a log file using Cambia.

    Args:
        logpath: Path to the log file to check.
        basepath: Base directory path containing audio files.

    Raises:
        ValueError: If log parsing fails, log is edited, or CRC mismatch detected.
        Exception: If any other error occurs during checking.
    """
    try:
        cambia_output = cambia.parse_log_file(logpath)

        score = int(cambia_output.evaluation_combined[0].combined_score)
        if score < 100:
            click.secho(f"Log Score: {score} (The torrent will be trumpable)", fg="yellow", bold=True)
        else:
            click.secho(f"Log Score: {score}", fg="green")
    except Exception as e:
        click.secho(f"Error checking log {logpath}: {e}", fg="red")
        raise

    if cambia_output.parsed.parsed_logs[0].checksum.integrity == cambia.Integrity.Mismatch:
        raise EditedLogError("Edited logs")
    elif cambia_output.parsed.parsed_logs[0].checksum.integrity == cambia.Integrity.Unknown:
        click.secho("Lacking a valid checksum. The torrent will be marked as trumpable.", fg="yellow")

    # Get list of CRCs from the log file
    copy_crc_set = {track.test_and_copy.copy_hash for track in cambia_output.parsed.parsed_logs[0].tracks}

    # Get list of files to check
    files_to_check: list[str] = []
    for root, _folders, files_ in os.walk(basepath):
        for f in files_:
            if os.path.splitext(f.lower())[1] in {".flac", ".mp3", ".m4a"}:
                files_to_check.append(os.path.join(root, f))

    if not files_to_check:
        raise ValueError("No audio files found!")

    click.secho("\nVerifying audio file CRC values...", fg="cyan", bold=True)
    if cambia_output.parsed.parsed_logs[0].tracks[0].is_range:
        toc_entries = cambia_output.parsed.parsed_logs[0].toc.raw.entries

        # Log contains range rip CRC, but we have individual track files
        # Concatenate track files to recreate the original range rip for CRC verification
        range_crc = await _calculate_range_crc_async(files_to_check, toc_entries)
        crc_set = {range_crc}
    else:
        crc_results = await process_files(files_to_check, _calculate_file_crc_async, "Calculating CRC32 hashes")
        crc_set = set(crc_results)

    if not copy_crc_set.issubset(crc_set):
        raise CRCMismatchError("CRC Mismatch")

    click.secho("All CRC values match the log file.", fg="green")
