import os
import re

import anyio
import asyncclick as click

from salmon import cfg
from salmon.common.files import process_files

FLAC_IMPORTANT_REGEXES = [
    re.compile(".+\\.flac: testing,.*\x08ok"),
]

MP3_IMPORTANT_REGEXES = [
    re.compile(r"WARNING: .*"),
    re.compile(r"INFO: .*"),
]


def format_integrity(result: tuple[bool, str]) -> str:
    """Format the integrity check result for display.

    Args:
        result: Tuple of (passed, details_output).

    Returns:
        Styled string indicating pass or fail with optional details.
    """
    integrities, integrities_out = result
    if integrities:
        return click.style("Passed integrity check", fg="green")
    else:
        output = click.style("Failed integrity check", fg="red", bold=True)
        if integrities_out:
            output += f"\nDetails:\n{integrities_out}"
        return output


async def handle_integrity_check(path: str) -> None:
    """Handle the integrity check process including UI and sanitization.

    Args:
        path: Path to a file or directory to check.

    Raises:
        click.Abort: If the path is neither a file nor a directory.
    """
    if os.path.isfile(path):
        if not any(path.lower().endswith(ext) for ext in [".flac", ".mp3"]):
            click.secho(f"File '{path}' is not a FLAC or MP3 file.", fg="red", bold=True)
            return

        result = await check_integrity(path)
        click.echo(format_integrity(result))

        if (
            not result[0]
            and path.lower().endswith(".flac")
            and click.confirm(click.style("\nWould you like to sanitize the file?", fg="magenta"))
        ):
            click.secho("\nSanitizing file...", fg="cyan", bold=True)
            if await sanitize_integrity(path):
                click.secho("Sanitization complete", fg="green")
            else:
                click.secho("Sanitization failed", fg="red", bold=True)
    elif os.path.isdir(path):
        result = await check_integrity(path)
        click.echo(format_integrity(result))

        if not result[0] and click.confirm(
            click.style("\nWould you like to sanitize the failed FLAC files?", fg="magenta")
        ):
            click.secho("\nSanitizing FLAC files...", fg="cyan", bold=True)
            if await sanitize_integrity(path):
                click.secho("Sanitization complete", fg="green", bold=True)
            else:
                click.secho("Some files failed sanitization", fg="red", bold=True)
    else:
        raise click.Abort


async def check_integrity(path: str, _: int | None = None) -> tuple[bool, str]:
    """Check the integrity of audio files at the given path.

    Args:
        path: Path to a FLAC/MP3 file or a directory containing audio files.
        _: Unused index parameter for process_files compatibility.

    Returns:
        Tuple of (all_passed, details_output).

    Raises:
        click.Abort: If no audio files found or path is invalid.
    """
    if path.lower().endswith(".flac"):
        return await _check_flac_integrity(path)
    elif path.lower().endswith(".mp3"):
        return await _check_mp3_integrity(path)
    elif os.path.isdir(path):
        integrities_out: list[str] = []
        integrities = True
        audio_files: list[str] = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    audio_files.append(os.path.join(root, f))
        if not audio_files:
            click.secho("No audio files found in directory", fg="red", bold=True)
            raise click.Abort
        results = await process_files(audio_files, check_integrity, "Checking audio files")
        for integrity, integrity_out in results:
            integrities = integrities and integrity
            integrities_out.append(integrity_out)
        return integrities, "\n".join(integrities_out)
    raise click.Abort


async def _check_flac_integrity(path: str) -> tuple[bool, str]:
    """Check the integrity of a single FLAC file using the flac CLI.

    Args:
        path: Path to the FLAC file.

    Returns:
        Tuple of (passed, important_output_lines).
    """
    try:
        result = await anyio.run_process(["flac", "-wt", path], check=False)
        result_text = result.stdout.decode() if result.stdout else ""
        if result.stderr:
            result_text += result.stderr.decode()
        important_lines: list[str] = []
        for line in result_text.split("\n"):
            for important_lines_re in FLAC_IMPORTANT_REGEXES:
                if important_lines_re.match(line):
                    important_lines.append(line)
        return True, "\n".join(important_lines)
    except Exception:
        return False, click.style(f"{os.path.basename(path)}: Failed integrity", fg="red", bold=True)


async def _check_mp3_integrity(path: str) -> tuple[bool, str]:
    """Check the integrity of a single MP3 file using mp3val.

    Args:
        path: Path to the MP3 file.

    Returns:
        Tuple of (passed, important_output_lines).
    """
    try:
        result = await anyio.run_process(["mp3val", path], check=False)
        result_text = result.stdout.decode() if result.stdout else ""
        important_lines: list[str] = []
        for line in result_text.split("\n"):
            for important_lines_re in MP3_IMPORTANT_REGEXES:
                if important_lines_re.match(line):
                    important_lines.append(line)
        return True, "\n".join(important_lines)
    except Exception:
        return False, click.style(f"{os.path.basename(path)}: Failed integrity", fg="red", bold=True)


async def sanitize_integrity(path: str, _: int | None = None) -> bool:
    """Sanitize audio files by re-encoding to fix integrity issues.

    Args:
        path: Path to a FLAC/MP3 file or a directory containing audio files.
        _: Unused index parameter for process_files compatibility.

    Returns:
        True if all files sanitized successfully, False otherwise.

    Raises:
        click.Abort: If the path is neither a supported file nor a directory.
    """
    if path.lower().endswith(".flac"):
        return await _sanitize_flac(path)
    elif path.lower().endswith(".mp3"):
        return await _sanitize_mp3(path)
    elif os.path.isdir(path):
        integrities = True
        audio_files: list[str] = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    audio_files.append(os.path.join(root, f))
        if not audio_files:
            return True
        results = await process_files(audio_files, sanitize_integrity, "Sanitizing audio files")
        for integrity in results:
            integrities = integrities and integrity
        return integrities
    raise click.Abort


async def _sanitize_flac(path: str) -> bool:
    """Sanitize a FLAC file by re-encoding and cleaning metadata.

    Args:
        path: Path to the FLAC file.

    Returns:
        True if sanitization succeeded, False otherwise.
    """
    try:
        os.rename(path, path + ".corrupted")
        result = await anyio.run_process(
            ["flac", f"-{cfg.upload.compression.flac_compression_level}", path + ".corrupted", "-o", path],
            check=False,
        )
        if result.returncode != 0:
            stderr_text = result.stderr.decode() if result.stderr else ""
            stdout_text = result.stdout.decode() if result.stdout else ""
            raise Exception(f"FLAC encoding failed:\n{stdout_text}\n{stderr_text}")
        os.remove(path + ".corrupted")
        result = await anyio.run_process(
            ["metaflac", "--dont-use-padding", "--remove", "--block-type=PADDING,PICTURE", path],
            check=False,
        )
        if result.returncode != 0:
            raise Exception("Failed to remove FLAC metadata blocks")
        result = await anyio.run_process(
            ["metaflac", "--add-padding=8192", path],
            check=False,
        )
        if result.returncode != 0:
            raise Exception("Failed to add FLAC padding")
        return True
    except Exception as e:
        click.secho(f"Failed to sanitize {path}, {e}", fg="red", bold=True)
        return False


async def _sanitize_mp3(path: str) -> bool:
    """Sanitize an MP3 file using mp3val to fix structural issues.

    Args:
        path: Path to the MP3 file.

    Returns:
        True if sanitization succeeded, False otherwise.
    """
    backup_path = path + ".corrupted"
    try:
        os.rename(path, backup_path)

        result = await anyio.run_process(
            ["mp3val", "-f", "-si", "-nb", "-t", backup_path],
            check=False,
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
            stderr_text = result.stderr.decode() if result.stderr else ""
            raise Exception(f"mp3val failed with return code {result.returncode}: {stderr_text}")

    except Exception as e:
        click.secho(f"Failed to sanitize {path}, {e}", fg="red", bold=True)
        # Ensure we restore the original file if something went wrong
        if os.path.exists(backup_path) and not os.path.exists(path):
            os.rename(backup_path, path)
        return False
