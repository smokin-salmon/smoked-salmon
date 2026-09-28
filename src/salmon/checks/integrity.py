import os
import re

import anyio
import asyncclick as click
import msgspec

from salmon import cfg
from salmon.common.files import process_files

FLAC_IMPORTANT_REGEXES = [
    # A warning, then flac's "ok" for the decode.
    re.compile(r"(.+\.flac:.+)\nok\s*", re.MULTILINE),
    # What stopped the decode: "a.flac: ERROR while decoding data".
    re.compile(r"^(.+: ERROR.*?)\s*$", re.MULTILINE),
]

FLAC_MD5_UNSET_RE = re.compile(r"WARNING.*MD5 signature.*STREAMINFO", re.IGNORECASE)

# flac prints "ok" only once the audio has decoded: "a.flac: ok" for a clean file, the MD5 warning then a
# bare "ok" when the MD5 is unset, and no "ok" at all when decoding fails.
FLAC_OK_RE = re.compile(r"^(?:.*: )?ok\s*$", re.MULTILINE)

# Said once for the whole album rather than once per file.
MD5_UNSET_NOTE = (
    "The audio decodes cleanly; there is no checksum stored to verify it against, "
    "which is common for WEB downloads.\n"
    "Sanitizing re-encodes the files losslessly to set the MD5 (this also strips embedded art)."
)

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
        md5_unset: Names of FLAC files with no MD5 signature in STREAMINFO. The explanation is
            MD5_UNSET_NOTE, shown once for all of them.
        decode_failures: Names of files that are not usable audio. These block an upload even when the
            user declines to sanitize; a failed file not listed here can be acknowledged.
        checked: How many files were checked.
    """

    passed: bool
    details: str = ""
    concerns: tuple[str, ...] = ()
    md5_unset: tuple[str, ...] = ()
    decode_failures: tuple[str, ...] = ()
    checked: int = 0


def _resolve_overstrikes(text: str) -> str:
    """Apply the backspaces flac prints over its progress instead of passing them on.

    flac 1.3 erases "testing, N% complete" with a run of \\x08 before writing its next message, even when
    its output is not a terminal. Passed through raw, they eat the characters in front of them wherever
    the details are shown.

    Args:
        text: flac's output.

    Returns:
        The text as a terminal would have left it.
    """
    out: list[str] = []
    for char in text:
        if char == "\x08":
            if out and out[-1] != "\n":
                out.pop()
        elif char == "\r":
            while out and out[-1] != "\n":
                out.pop()
        else:
            out.append(char)
    return "".join(out)


def md5_unset_summary(count: int, checked: int) -> str:
    """Say how many files have no MD5, out of how many were checked."""
    if checked:
        return f"{count} of {checked} file(s) have no MD5 signature stored"
    return f"{count} file(s) have no MD5 signature stored"


def sanitize_prompt(result: IntegrityResult) -> str:
    """The sanitize question, naming the reason it is asked when it is only a missing MD5."""
    if result.md5_unset and not result.decode_failures:
        return (
            f"{md5_unset_summary(len(result.md5_unset), result.checked)}. "
            "Re-encode them losslessly to set it? (also strips embedded art)"
        )
    return "Do you want to sanitize this upload?"


def _describe_md5_unset(names: tuple[str, ...], checked: int) -> str:
    """The unset-MD5 section, once for the whole set, naming the files only when there are few."""
    head = md5_unset_summary(len(names), checked)
    if len(names) <= 3:
        head += f": {', '.join(names)}"
    return f"{head}.\n{MD5_UNSET_NOTE}"


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
        # A missing checksum and a file that will not decode both fail the check, but only the second
        # means the audio is suspect: say which in the headline.
        if result.md5_unset and not result.decode_failures:
            output = click.style("Integrity check not passed: no MD5 signature stored", fg="red", bold=True)
        else:
            output = click.style("Failed integrity check", fg="red", bold=True)
        if result.md5_unset:
            output += "\n\n" + click.style(_describe_md5_unset(result.md5_unset, result.checked), fg="yellow")
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
            and click.confirm(click.style(f"\n{sanitize_prompt(result)}", fg="magenta"))
        ):
            await sanitize_and_verify(path)
    elif os.path.isdir(path):
        result = await check_integrity(path)
        click.echo(format_integrity(result))

        if not result.passed and click.confirm(click.style(f"\n{sanitize_prompt(result)}", fg="magenta")):
            await sanitize_and_verify(path)
    else:
        raise click.Abort


async def resolve_integrity_for_upload(path: str, *, scene: bool, assume_yes: bool) -> IntegrityResult:
    """Check the folder before an upload, offer to sanitize, and abort if a file still will not decode.

    Args:
        path: The release folder.
        scene: Whether this is a scene release, which must not be sanitized.
        assume_yes: Sanitize without asking (upload.yes_all).

    Returns:
        The last integrity result: after sanitizing, the re-check's.

    Raises:
        click.Abort: If a scene release fails the check, or a file does not decode.
    """
    click.secho("\nChecking integrity of audio files...", fg="cyan", bold=True)
    result = await check_integrity(path)
    click.echo(format_integrity(result))
    if result.passed:
        return result

    if scene:
        click.secho(
            "Some files failed the integrity check, and this is a scene release. "
            "You need to sanitize and de-scene before uploading. Aborting.",
            fg="red",
            bold=True,
        )
        raise click.Abort()

    if assume_yes or click.confirm(click.style(f"\n{sanitize_prompt(result)}", fg="magenta"), default=True):
        result = await sanitize_and_verify(path)

    # Declining to sanitize is not consent to upload files that do not decode, and yes_all takes the
    # default answer, not "upload anything".
    if result.decode_failures:
        click.secho(
            f"{len(result.decode_failures)} file(s) do not decode: {', '.join(result.decode_failures)}. Aborting.",
            fg="red",
            bold=True,
        )
        raise click.Abort()
    return result


async def sanitize_and_verify(path: str) -> IntegrityResult:
    """Sanitize, then check again and report what the re-check found.

    sanitize_integrity succeeding is not evidence that the files now pass: the re-check is.

    Args:
        path: Path to a FLAC/MP3 file or a directory containing audio files.

    Returns:
        The re-check's result.
    """
    click.secho("\nSanitizing files...", fg="cyan", bold=True)
    reported_ok = await sanitize_integrity(path)
    click.secho("\nChecking integrity again...", fg="cyan", bold=True)
    result = await check_integrity(path)
    click.echo(format_integrity(result))
    if not result.passed:
        click.secho("Sanitization did not clear the integrity check.", fg="red", bold=True)
    elif not reported_ok:
        click.secho("A file reported a sanitization error, but the check now passes.", fg="yellow")
    else:
        click.secho("Sanitization complete", fg="green")
    return result


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
        md5_unset: list[str] = []
        decode_failures: list[str] = []
        for result in results:
            integrities = integrities and result.passed
            if result.details:
                integrities_out.append(result.details)
            concerns.extend(result.concerns)
            md5_unset.extend(result.md5_unset)
            decode_failures.extend(result.decode_failures)
        return IntegrityResult(
            integrities,
            "\n".join(integrities_out),
            tuple(concerns),
            tuple(md5_unset),
            tuple(decode_failures),
            checked=sum(result.checked for result in results),
        )
    raise click.Abort


async def _check_flac_integrity(path: str) -> IntegrityResult:
    """Check the integrity of a single FLAC file using the flac CLI.

    Args:
        path: Path to the FLAC file.

    Returns:
        The file's result.
    """
    name = os.path.basename(path)
    try:
        result = await anyio.run_process(["flac", "-wt", path], check=False)
    except Exception:
        return IntegrityResult(
            False, click.style(f"{name}: Failed integrity", fg="red", bold=True), decode_failures=(name,), checked=1
        )
    result_text = result.stdout.decode(errors="replace") if result.stdout else ""
    if result.stderr:
        result_text += result.stderr.decode(errors="replace")
    result_text = _resolve_overstrikes(result_text)
    important_matches: list[str] = []
    for important_re in FLAC_IMPORTANT_REGEXES:
        important_matches.extend(m.strip() for m in important_re.findall(result_text))
    md5_unset = bool(FLAC_MD5_UNSET_RE.search(result_text))
    # md5_unset carries that fact; keeping flac's warning line too shows it twice.
    important_matches = [m for m in important_matches if not FLAC_MD5_UNSET_RE.search(m)]
    passed = result.returncode == 0 and not md5_unset
    # -w makes the MD5 warning exit non-zero, so only flac's "ok" says the audio decoded.
    md5_only = md5_unset and bool(FLAC_OK_RE.search(result_text))
    return IntegrityResult(
        passed,
        "\n".join(important_matches),
        md5_unset=(name,) if md5_unset else (),
        decode_failures=() if passed or md5_only else (name,),
        checked=1,
    )


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
            False, click.style(f"{name}: Failed integrity", fg="red", bold=True), decode_failures=(name,), checked=1
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
        checked=1,
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
    backup_path = path + ".corrupted"
    try:
        os.rename(path, backup_path)
        result = await anyio.run_process(
            ["flac", f"-{cfg.upload.compression.flac_compression_level}", backup_path, "-o", path],
            check=False,
        )
        if result.returncode != 0:
            stderr_text = result.stderr.decode() if result.stderr else ""
            stdout_text = result.stdout.decode() if result.stdout else ""
            raise Exception(f"FLAC encoding failed:\n{stdout_text}\n{stderr_text}")
        os.remove(backup_path)
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
        # flac deletes its output when the re-encode fails: put the original back, or the file is gone from
        # the release and the check after sanitizing passes without it.
        if os.path.exists(backup_path):
            os.replace(backup_path, path)
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
