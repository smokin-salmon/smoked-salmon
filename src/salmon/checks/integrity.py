import os
import re

import anyio
import asyncclick as click
import msgspec

from salmon import cfg
from salmon.common.files import process_files

FLAC_IMPORTANT_REGEXES = [
    re.compile(r"(.+\.flac: testing,.*)\x08ok"),
    re.compile(r"(.+\.flac:.+)\nok\s*", re.MULTILINE),
]

FLAC_MD5_UNSET_RE = re.compile(r"WARNING.*MD5 signature.*STREAMINFO", re.IGNORECASE)

# mp3val exits 0 whatever it finds, so its messages are the verdict. It reports each finding as
# `WARNING: "<path>" (offset 0x...): <message>` or `ERROR: "<path>": <message>`, then one
# `INFO: "<path>": <summary>` line for a file it could analyse.
MP3VAL_LINE_RE = re.compile(r'^(INFO|WARNING|ERROR): ".*"(?: \((offset 0x[0-9a-fA-F]+)\))?: (.*)$')

# The file is not MPEG audio at all: nothing to repair, the upload stops.
MP3VAL_NOT_AUDIO = (
    "Unknown file format",
    "This is a RIFF file, not MPEG stream",
    "Too few MPEG frames",
)

# About tags or seeking, not the audio: reported, but the file passes.
MP3VAL_INFORMATIONAL = (
    "No supported tags in the file",
    "VBR detected, but no VBR header is present",
    "Several APEv2 tags in one file",
)


class IntegrityResult(msgspec.Struct, frozen=True):
    """Whether the files passed, and what the tools said.

    Args:
        passed: True if every file passed.
        details: The tools' messages worth reading, one per line.
        concerns: Informational messages about files that still passed.
        decode_failures: Names of files that are not usable audio. These block an upload even when the
            user declines to sanitize; a failed file not listed here can be acknowledged.
    """

    passed: bool
    details: str = ""
    concerns: tuple[str, ...] = ()
    decode_failures: tuple[str, ...] = ()


def format_integrity(result: IntegrityResult) -> str:
    """Format the integrity check result for display.

    Args:
        result: The integrity check result.

    Returns:
        Styled string indicating pass or fail with optional details.
    """
    integrities, integrities_out = result.passed, result.details
    if integrities:
        if result.concerns:
            return click.style(
                f"Passed integrity check, with {len(result.concerns)} note(s):\n" + "\n".join(result.concerns),
                fg="yellow",
            )
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
            not result.passed
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

        if not result.passed and click.confirm(
            click.style("\nWould you like to sanitize the failed FLAC files?", fg="magenta")
        ):
            click.secho("\nSanitizing FLAC files...", fg="cyan", bold=True)
            if await sanitize_integrity(path):
                click.secho("Sanitization complete", fg="green", bold=True)
            else:
                click.secho("Some files failed sanitization", fg="red", bold=True)
    else:
        raise click.Abort


async def check_integrity(path: str, _: int | None = None) -> IntegrityResult:
    """Check the integrity of audio files at the given path.

    Args:
        path: Path to a FLAC/MP3 file or a directory containing audio files.
        _: Unused index parameter for process_files compatibility.

    Returns:
        The combined result for every file checked.

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
        concerns: list[str] = []
        decode_failures: list[str] = []
        for result in results:
            integrities = integrities and result.passed
            if result.details:
                integrities_out.append(result.details)
            concerns.extend(result.concerns)
            decode_failures.extend(result.decode_failures)
        return IntegrityResult(integrities, "\n".join(integrities_out), tuple(concerns), tuple(decode_failures))
    raise click.Abort


async def _check_flac_integrity(path: str) -> IntegrityResult:
    """Check the integrity of a single FLAC file using the flac CLI.

    Args:
        path: Path to the FLAC file.

    Returns:
        The file's result.
    """
    try:
        result = await anyio.run_process(["flac", "-wt", path], check=False)
        result_text = result.stdout.decode() if result.stdout else ""
        if result.stderr:
            result_text += result.stderr.decode()
        important_matches: list[str] = []
        for important_re in FLAC_IMPORTANT_REGEXES:
            important_matches.extend(m.strip() for m in important_re.findall(result_text))
        md5_unset = FLAC_MD5_UNSET_RE.search(result_text)
        if md5_unset:
            important_matches.append(f"{os.path.basename(path)}: MD5 signature unset in STREAMINFO: sanitize to fix")
        passed = result.returncode == 0 and not md5_unset
        return IntegrityResult(passed, "\n".join(important_matches))
    except Exception:
        return IntegrityResult(False, click.style(f"{os.path.basename(path)}: Failed integrity", fg="red", bold=True))


async def _check_mp3_integrity(path: str) -> IntegrityResult:
    """Check the integrity of a single MP3 file using mp3val.

    mp3val's findings sort three ways: a file that is not MPEG audio fails and blocks the upload, stream
    damage (truncation, resynchronisation, garbage, CRC or VBR header mismatches) fails but can be
    repaired by sanitizing or acknowledged, and notes about tags or seeking pass. A run that analysed
    nothing fails too, so an unreadable file cannot pass.

    Args:
        path: Path to the MP3 file.

    Returns:
        The file's result.
    """
    name = os.path.basename(path)
    try:
        result = await anyio.run_process(["mp3val", path], check=False)
    except Exception:
        return IntegrityResult(
            False, click.style(f"{name}: Failed integrity", fg="red", bold=True), decode_failures=(name,)
        )
    result_text = result.stdout.decode(errors="replace") if result.stdout else ""
    if result.stderr:
        result_text += result.stderr.decode(errors="replace")

    details: list[str] = []
    concerns: list[str] = []
    analysed = False
    damaged = False
    not_audio = False
    for line in result_text.splitlines():
        match = MP3VAL_LINE_RE.match(line.strip())
        if not match:
            continue
        level, offset, message = match.groups()
        described = f"{name} ({offset}): {message}" if offset else f"{name}: {message}"
        if level == "INFO":
            analysed = True
            if message.startswith("No MPEG frames"):
                not_audio = True
                details.append(described)
        elif level == "ERROR" or message.startswith(MP3VAL_NOT_AUDIO):
            not_audio = True
            details.append(described)
        elif message.startswith(MP3VAL_INFORMATIONAL):
            concerns.append(described)
        else:
            damaged = True
            details.append(described)

    if not analysed and not not_audio:
        not_audio = True
        details.append(f"{name}: mp3val did not analyse the file: {result_text.strip() or 'no output'}")
    passed = result.returncode == 0 and not damaged and not not_audio
    return IntegrityResult(
        passed,
        "\n".join(details),
        tuple(concerns),
        decode_failures=(name,) if not_audio or (not passed and not damaged) else (),
    )


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
