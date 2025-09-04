import os
import re
import subprocess

import click

from salmon import cfg
from salmon.common.figles import process_files

FLAC_IMPORTANT_REGEXES = [
    re.compile(".+\\.flac: testing,.*\x08ok"),
]

MP3_IMPORTANT_REGEXES = [
    re.compile(r"WARNING: .*"),
    re.compile(r"INFO: .*"),
]


def format_integrity(result):
    """Format the integrity check result for display"""
    integrities, integrities_out = result
    if integrities:
        return click.style("Passed integrity check", fg="green")
    else:
        output = click.style("Failed integrity check", fg="red", bold=True)
        if integrities_out:
            output += f"\nDetails:\n{integrities_out}"
        return output


def handle_integrity_check(path):
    """Handle the integrity check process including UI and sanitization"""
    if os.path.isfile(path):
        if not any(path.lower().endswith(ext) for ext in [".flac", ".mp3"]):
            click.secho(f"File '{path}' is not a FLAC or MP3 file.", fg="red", bold=True)
            return

        result = check_integrity(path)
        click.echo(format_integrity(result))

        if (
            not result[0]
            and path.lower().endswith(".flac")
            and click.confirm(click.style("\nWould you like to sanitize the file?", fg="magenta"))
        ):
            click.secho("\nSanitizing file...", fg="cyan", bold=True)
            if sanitize_integrity(path):
                click.secho("Sanitization complete", fg="green")
            else:
                click.secho("Sanitization failed", fg="red", bold=True)
    elif os.path.isdir(path):
        result = check_integrity(path)
        click.echo(format_integrity(result))

        if not result[0] and click.confirm(
            click.style("\nWould you like to sanitize the failed FLAC files?", fg="magenta")
        ):
            click.secho("\nSanitizing FLAC files...", fg="cyan", bold=True)
            if sanitize_integrity(path):
                click.secho("Sanitization complete", fg="green", bold=True)
            else:
                click.secho("Some files failed sanitization", fg="red", bold=True)
    else:
        raise click.Abort


def check_integrity(path, _=None):
    if path.lower().endswith(".flac"):
        return _check_flac_integrity(path)
    elif path.lower().endswith(".mp3"):
        return _check_mp3_integrity(path)
    elif os.path.isdir(path):
        integrities_out = []
        integrities = True
        audio_files = []
        for root, _, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    audio_files.append(os.path.join(root, f))
        if not audio_files:
            click.secho("No audio files found in directory", fg="red", bold=True)
            raise click.Abort
        results = process_files(audio_files, check_integrity, "Checking audio files")
        for integrity, integrity_out in results:
            integrities = integrities and integrity
            integrities_out.append(integrity_out)
        return integrities, "\n".join(integrities_out)
    raise click.Abort


def _check_flac_integrity(path):
    try:
        result = subprocess.check_output(["flac", "-wt", path], stderr=subprocess.STDOUT, text=True)
        important_lines = []
        for line in result.split("\n"):
            for important_lines_re in FLAC_IMPORTANT_REGEXES:
                if important_lines_re.match(line):
                    important_lines.append(line)
        return True, "\n".join(important_lines)
    except Exception:
        return False, click.style(f"{os.path.basename(path)}: Failed integrity", fg="red", bold=True)


def _check_mp3_integrity(path):
    try:
        result = subprocess.check_output(["mp3val", path], text=True)
        important_lines = []
        for line in result.split("\n"):
            for important_lines_re in MP3_IMPORTANT_REGEXES:
                if important_lines_re.match(line):
                    important_lines.append(line)
        return True, "\n".join(important_lines)
    except Exception:
        return False, click.style(f"{os.path.basename(path)}: Failed integrity", fg="red", bold=True)


def sanitize_integrity(path, _=None):
    if path.lower().endswith(".flac"):
        return _sanitize_flac(path)
    elif path.lower().endswith(".mp3"):
        return _sanitize_mp3(path)
    elif os.path.isdir(path):
        integrities = True
        audio_files = []
        for root, _, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    audio_files.append(os.path.join(root, f))
        if not audio_files:
            return True
        results = process_files(audio_files, sanitize_integrity, "Sanitizing audio files")
        for integrity in results:
            integrities = integrities and integrity
        return integrities
    raise click.Abort


def _sanitize_flac(path):
    try:
        os.rename(path, path + ".corrupted")
        result = subprocess.run(
            ["flac", f"-{cfg.upload.compression.flac_compression_level}", path + ".corrupted", "-o", path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise Exception(f"FLAC encoding failed:\n{result.stdout}\n{result.stderr}")
        os.remove(path + ".corrupted")
        result = subprocess.run(
            ["metaflac", "--dont-use-padding", "--remove", "--block-type=PADDING,PICTURE", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise Exception("Failed to remove FLAC metadata blocks")
        result = subprocess.run(
            ["metaflac", "--add-padding=8192", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if result.returncode != 0:
            raise Exception("Failed to add FLAC padding")
        return True
    except Exception as e:
        click.secho(f"Failed to sanitize {path}, {e}", fg="red", bold=True)
        return False


def _sanitize_mp3(path):
    try:
        backup_path = path + ".corrupted"
        os.rename(path, backup_path)

        result = subprocess.run(
            ["mp3val", "-f", "-si", "-nb", "-t", backup_path],
            capture_output=True,
            text=True,
        )

        if os.path.exists(backup_path):
            os.rename(backup_path, path)

        # Check if the operation was successful
        if result.returncode == 0:
            return True
        else:
            # If mp3val failed, restore the original file
            if os.path.exists(backup_path) and not os.path.exists(path):
                os.rename(backup_path, path)
            raise Exception(f"mp3val failed with return code {result.returncode}: {result.stderr}")

    except Exception as e:
        click.secho(f"Failed to sanitize {path}, {e}", fg="red", bold=True)
        # Ensure we restore the original file if something went wrong
        if os.path.exists(backup_path) and not os.path.exists(path):
            os.rename(backup_path, path)
        return False
