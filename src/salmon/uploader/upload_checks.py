"""The checks an upload makes on a release before it goes up, and the refusals they lead to."""

import os
from typing import Any

import asyncclick as click

import salmon.trackers
from salmon import interaction
from salmon.checks.do_not_upload import Candidate, do_not_upload_reason
from salmon.checks.high_rate import sixteen_bit_notice
from salmon.checks.logs import check_log_cambia
from salmon.errors import CRCMismatchError, EditedLogError, LogCheckSkipped
from salmon.localization import text as _zh
from salmon.uploader.spectrals import prompt_lossy_master


async def _check_logs(path: str) -> None:
    """Score every rip log under the album and check its CRCs against the audio.

    Args:
        path: Album folder.

    Raises:
        click.Abort: If a log was edited, the audio could not be verified, or the user declines to
            continue after a CRC mismatch.
    """
    click.secho(_zh("\nChecking logs"), fg="green")

    def _abort_on_scan_error(error: OSError) -> None:
        # os.walk would otherwise skip the folder, and its log, silently.
        click.secho(_zh(f"Could not scan {error.filename} for logs: {error}"), fg="red")
        raise click.Abort() from error

    for root, _, files in os.walk(path, onerror=_abort_on_scan_error):
        for f in files:
            if not f.lower().endswith(".log"):
                continue
            filepath = os.path.join(root, f)
            click.secho(_zh(f"\nScoring {filepath}..."), fg="cyan", bold=True)
            try:
                await check_log_cambia(filepath, path)
            except EditedLogError as e:
                click.secho(_zh("Error: Edited logs detected!"), fg="red", bold=True)
                raise click.Abort() from e
            except CRCMismatchError as e:
                click.secho(_zh("Error: CRC mismatch between log and audio files!"), fg="red", bold=True)
                if not await interaction.confirm(
                    _zh(
                        click.style(
                            _zh("Log file CRC does not match audio files. Do you want to continue upload anyway?"),
                            fg="magenta",
                        )
                    ),
                    default=False,
                ):
                    raise click.Abort() from e
            except LogCheckSkipped as e:
                click.secho(_zh(f"Log not checked: {e}"), fg="yellow")
            except Exception as e:
                # Any other failure is one while verifying the audio, which must not pass as verified.
                click.secho(_zh(f"Could not verify the audio against {filepath}: {e}"), fg="red")
                raise click.Abort() from e


def _do_not_upload_refusal(tracker: str, release: dict[str, Any], said: str | None = None) -> str | None:
    """Why the tracker's Do-Not-Upload list forbids the release, if it does, saying it unless it was said already.

    Nothing skips it, -yyy included.

    Args:
        release: The release's rls_data or metadata.
        said: The reason a check of the same tracker gave before the review, if any.
    """
    reason = do_not_upload_reason(tracker, Candidate.from_metadata(release))
    if reason is not None and reason != said:
        click.secho(_zh(f"\nNot uploading to {tracker}: {reason}"), fg="red", bold=True)
    elif reason is None and said is not None:
        click.secho(_zh(f"\nAs reviewed, the release is not on {tracker}'s Do-Not-Upload list."), fg="yellow")
    return reason


def _sixteen_bit_refusal(tracker: str, audio_info: dict[str, Any]) -> bool:
    """Say what the tracker's rule on 16bit files above 48 kHz means for the files, and whether it refuses them.

    A tracker that only trumps them gets a warning and the upload goes on. Nothing skips a refusal, -yyy included.
    """
    site_class = salmon.trackers.tracker_classes.get(tracker)
    rule = site_class.TAG_RULES.sixteen_bit_above_48khz if site_class else ""
    notice = sixteen_bit_notice(tracker, rule, audio_info)
    if notice is None:
        return False
    if rule == "refused":
        click.secho(_zh(f"\nNot uploading to {tracker}: {notice}"), fg="red", bold=True)
        return True
    click.secho(f"\n{notice}", fg="yellow")
    return False


def _warn_about_provenance(provenance: dict[str, Any]) -> None:
    """Print each ripper or store marker in the tags that the audio contradicts.

    `provenance` is `gather_provenance` of the files as they were given: read before they are retagged, which can
    blank or replace their comments. Only warns: it never stops the upload or changes an answer, and prints
    nothing when no marker contradicts the audio.
    """
    if not provenance["files"]:
        click.secho(_zh("\nTag markers not checked: the tags could not be read."), fg="yellow")
        return
    if contradictions := provenance["contradictions"]:
        click.secho(_zh("\nTag markers the audio contradicts:"), fg="yellow", bold=True)
        for note in contradictions:
            click.secho(f"  - {note}", fg="yellow")


async def _lossy_master_without_spectrals(lossy: bool | None, format: str) -> bool:
    """Whether the release is lossy mastered, when its spectrals were given by URL: none are made to look at.

    Asked as check_spectrals asks it, unless --lossy or --not-lossy said; only a FLAC is asked about.
    """
    click.secho(_zh("\nSpectrals given by URL (--spectral-url): none are made, shown or uploaded."), fg="cyan")
    if lossy is not None or format != "FLAC":
        return bool(lossy)
    while (answer := await prompt_lossy_master()) is None:
        click.secho(_zh("There are no spectrals to reopen: they were given by URL."), fg="yellow")
    return answer
