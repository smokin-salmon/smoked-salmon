import json
import os
import shutil
import subprocess

import click

from salmon.common.figles import process_files


def is_sublist(*, sub, main):
    return all(elem in main for elem in sub)


def _calculate_file_crc(filepath, _ = None):
    """Calculate CRC32 hash for a single audio file."""
    output = subprocess.check_output(
        [
            "ffmpeg",
            "-i",
            filepath,
            "-nostdin",
            "-map",
            "0:0",
            "-f",
            "hash",
            "-hash",
            "crc32",
            "-",
        ],
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    return output.strip().removeprefix("CRC32=").upper()


def check_log_cambia(logpath, basepath):
    """Check a log file using Cambia."""

    path_has_cambia = shutil.which("cambia")
    if not path_has_cambia:
        click.secho("Cambia is not on the system PATH. Skipping log check!", fg="yellow")
        return

    try:
        cambia_output = json.loads(
            subprocess.check_output(
                ["cambia", "-p", logpath],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
        )
        score = int(cambia_output['evaluation_combined'][0]['combined_score'])
        if score < 100:
            click.secho(
                f"Log Score: {score} (The torrent will be trumpable)",
                fg="yellow",
                bold=True
            )
        else:
            click.secho(
                f"Log Score: {score}",
                fg="green"
            )
    except Exception as e:
        click.secho(f"Error checking log {logpath}: {e}", fg="red")
        raise

    if cambia_output['parsed']['parsed_logs'][0]['checksum']['integrity'] == "Mismatch":
        raise ValueError("Edited logs!")
    elif cambia_output['parsed']['parsed_logs'][0]['checksum']['integrity'] == 'Unknown':
        click.secho("Lacking a valid checksum. The torrent will be marked as trumpable.", fg="yellow")

    # Get list of CRCs from the log file
    copy_crc_list = [
        track['test_and_copy']['copy_hash']
        for track in cambia_output['parsed']['parsed_logs'][0]['tracks']
    ]

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
