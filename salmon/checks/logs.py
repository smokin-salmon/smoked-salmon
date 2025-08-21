import os

import cambia
import click
import ffmpeg

from salmon.common.figles import process_files


def is_sublist(*, sub, main):
    return all(elem in main for elem in sub)


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
        raise ValueError("Edited logs!")
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
    crc_list = process_files(files_to_check, _calculate_file_crc, "Calculating CRC32 hashes")

    if not is_sublist(sub=copy_crc_list, main=crc_list):
        raise ValueError("CRC Mismatch!")

    click.secho("All CRC values match the log file.", fg="green")
