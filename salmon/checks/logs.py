import os

import cambia
import click
import ffmpeg

from salmon.common.figles import process_files


def is_sublist(*, sub, main):
    return all(elem in main for elem in sub)


def _get_audio_duration_sectors(filepath):
    """Get the duration of an audio file in CD sectors using ffprobe.

    Uses duration_ts to calculate precise sector count:
    duration_ts * 75 / 44100 = sectors (since 44100 samples/sec ÷ 75 sectors/sec = 588 samples/sector)
    """
    try:
        probe = ffmpeg.probe(filepath)
        audio_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
        duration_sectors = int(audio_stream["duration_ts"] * 75 / 44100)
        return duration_sectors
    except (ffmpeg.Error, KeyError, StopIteration) as e:
        raise RuntimeError(f"Error getting duration in sectors for {filepath}: {e}") from e


def _calculate_file_crc(filepath, _=None):
    """Calculate CRC32 hash for a single audio file."""
    try:
        out, _ = (
            ffmpeg.input(filepath)
            .output("pipe:", format="hash", hash="crc32", map="0:0")
            .global_args("-nostdin")
            .run(capture_stdout=True, capture_stderr=True)
        )
        return out.decode("utf-8").strip().removeprefix("CRC32=").upper()
    except ffmpeg.Error as e:
        raise RuntimeError(f"FFmpeg error calculating CRC32 for {filepath}: {e}") from e


def _calculate_range_crc(track_files, toc_entries):
    """Calculate CRC32 hash by concatenating individual track files to recreate the original range rip."""
    try:
        # Sort track files by track number to match TOC entries order
        # Assuming files are named like "01 - track.flac", "02 - track.flac", etc.
        sorted_files = sorted(track_files, key=lambda f: os.path.basename(f))

        if len(sorted_files) != len(toc_entries):
            raise ValueError(f"Mismatch: {len(sorted_files)} track files but {len(toc_entries)} TOC entries")

        # Create input streams with proper timing based on TOC sector entries
        input_streams = []

        for track_file, toc_entry in zip(sorted_files, toc_entries, strict=True):
            # Use sector information for precise positioning
            track_length_sectors = toc_entry["end_sector"] - toc_entry["start_sector"] + 1

            # Get actual audio file duration in sectors
            actual_duration_sectors = _get_audio_duration_sectors(track_file)

            # Add the track itself
            track_input = ffmpeg.input(track_file)
            input_streams.append(track_input)

            # If there's a gap after this track, add silence
            if track_length_sectors > actual_duration_sectors:
                gap_sectors = track_length_sectors - actual_duration_sectors
                gap_samples = int(gap_sectors * 44100 / 75)

                if gap_sectors > 0:
                    click.secho(
                        f"Adding {gap_sectors / 75:.2f}s silence gap ({gap_sectors} sectors, {gap_samples} samples)",
                        fg="yellow",
                    )
                    silence = ffmpeg.input(f"anullsrc=r=44100:cl=stereo:nb_samples={gap_samples}", f="lavfi")
                    input_streams.append(silence)

        # Concatenate all streams (tracks + silence gaps) to recreate the original range rip
        concat_stream = input_streams[0] if len(input_streams) == 1 else ffmpeg.concat(*input_streams, v=0, a=1)

        # Calculate CRC32 hash of the concatenated stream (should match the range rip CRC in log)
        out, _ = (
            concat_stream.output("pipe:", format="hash", hash="crc32")
            .global_args("-nostdin")
            .run(capture_stdout=True, capture_stderr=True)
        )

        return out.decode("utf-8").strip().removeprefix("CRC32=").upper()

    except ffmpeg.Error as e:
        stderr_output = e.stderr.decode("utf-8") if e.stderr else "No stderr output"
        raise RuntimeError(f"FFmpeg error calculating range CRC32: {e}\nStderr: {stderr_output}") from e


def check_log_cambia(logpath, basepath):
    """Check a log file using Cambia."""
    try:
        cambia_output = cambia.parse_file(logpath)

        if not cambia_output["success"]:
            raise ValueError(f"Cambia parsing failed for '{logpath}': {cambia_output['error']}")

        log_data = cambia_output["data"]

        score = int(log_data["evaluation_combined"][0]["combined_score"])
        if score < 100:
            click.secho(f"Log Score: {score} (The torrent will be trumpable)", fg="yellow", bold=True)
        else:
            click.secho(f"Log Score: {score}", fg="green")
    except Exception as e:
        click.secho(f"Error checking log {logpath}: {e}", fg="red")
        raise

    if log_data["parsed"]["parsed_logs"][0]["checksum"]["integrity"] == "Mismatch":
        raise ValueError("Edited logs")
    elif log_data["parsed"]["parsed_logs"][0]["checksum"]["integrity"] == "Unknown":
        click.secho("Lacking a valid checksum. The torrent will be marked as trumpable.", fg="yellow")

    # Get list of CRCs from the log file
    copy_crc_list = [track["test_and_copy"]["copy_hash"] for track in log_data["parsed"]["parsed_logs"][0]["tracks"]]

    # Get list of files to check
    files_to_check = []
    for root, _folders, files_ in os.walk(basepath):
        for f in files_:
            if os.path.splitext(f.lower())[1] in {".flac", ".mp3", ".m4a"}:
                files_to_check.append(os.path.join(root, f))

    if not files_to_check:
        raise ValueError("No audio files found!")

    click.secho("\nVerifying audio file CRC values...", fg="cyan", bold=True)
    if log_data["parsed"]["parsed_logs"][0]["tracks"][0]["is_range"]:
        toc_entries = log_data["parsed"]["parsed_logs"][0]["toc"]["raw"]["entries"]

        # Log contains range rip CRC, but we have individual track files
        # Concatenate track files to recreate the original range rip for CRC verification
        range_crc = _calculate_range_crc(files_to_check, toc_entries)
        crc_list = [range_crc]
    else:
        crc_list = process_files(files_to_check, _calculate_file_crc, "Calculating CRC32 hashes")

    if not is_sublist(sub=copy_crc_list, main=crc_list):
        raise ValueError("CRC Mismatch")

    click.secho("All CRC values match the log file.", fg="green")
