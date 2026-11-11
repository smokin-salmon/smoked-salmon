"""One tracker's turn in an upload run, and what the turns of a run share."""

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import asyncclick as click

from salmon import cfg, dryrun
from salmon.converter.downconverting import BitDepth, conversion_note
from salmon.converter.transcoding import Bitrate, transcode_note
from salmon.errors import RequestError, UnknownOutcomeError
from salmon.trackers.connection import print_certificate_hint
from salmon.uploader import record
from salmon.uploader.conversion_choice import (
    _applicable_conversions,
    _held_conversions,
    choose_conversions,
    get_downconversion_options,
)
from salmon.uploader.conversion_upload import execute_downconversion_tasks
from salmon.uploader.cover_upload import resolve_cover_url
from salmon.uploader.dupe_checker import print_torrents
from salmon.uploader.request_checker import check_requests
from salmon.uploader.seedbox import UploadManager
from salmon.uploader.spectrals import SpectralUploads, post_upload_spectral_check
from salmon.uploader.upload import upload_and_report

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


@dataclass(frozen=True, kw_only=True)
class ReleaseToUpload:
    """What the review settled, the same for every tracker of the run."""

    path: str
    metadata: dict[str, Any]
    rls_data: dict[str, Any]
    track_data: dict[str, Any]
    hybrid: bool
    searchstrs: list[str]
    source: str
    source_url: str | None
    conversion: dict[str, Any] | None
    conversions: tuple[str, ...] | None
    max_path_length: int
    source_flac: dict[str, Any] | None
    flac_url: str | None
    flac_group: dict[str, Any] | None
    given_cover_url: str | None
    remove_downloaded_cover_image: bool


@dataclass(kw_only=True)
class TrackerRun:
    """What one tracker's turn leaves for the next, changed in place: an abort mid-turn still finds what is up."""

    seedbox_uploader: UploadManager
    spectral_uploads: SpectralUploads
    request_id: int | str | None
    spectrals_after: bool
    lossy_master: bool
    lossy_comment: str | None
    spectral_urls: dict[int, list[str]] | None
    spectral_ids: dict[int, str] | None
    uploaded: list[str] = field(default_factory=list)  # The URL of each torrent uploaded, for an abort to list
    # Trackers whose upload failed or was skipped, or that are missing a conversion the run wanted: the folder is
    # needed to retry them. A first tracker refused before the loop is one of them.
    failed_trackers: list[str] = field(default_factory=list)
    cover_urls: dict[str, str | None] = field(default_factory=dict)  # Uploaded cover URL per image host


class Turn(enum.Enum):
    """How a tracker's turn ended, for the loop to choose what comes next."""

    ENDED = enum.auto()  # Ran to its end, whether or not anything went up: the loop may say it is done
    NO_COVER = enum.auto()  # Refused at the cover: skipped, and nothing was sent


def converted_from_note(conversion: dict[str, Any] | None, url: str | None) -> str | None:
    """The conversion note for a folder a converter made, worded as the in-run conversion upload's."""
    if not conversion:
        return None
    click.secho(f"\nThis folder was converted from {conversion.get('source')}; describing the conversion.", fg="cyan")
    if conversion.get("kind") == "transcode":
        return transcode_note(url or "", cast("Bitrate", conversion.get("bitrate")))
    return conversion_note(url or "", conversion.get("sample_rate"), cast("BitDepth", conversion.get("bit_depth", 16)))


async def upload_to_tracker(
    release: ReleaseToUpload, gazelle_site: "BaseGazelleApi", group_id: int | None, run: TrackerRun
) -> Turn:
    """Upload the release to one tracker: its cover, spectrals, request, torrent and conversions.

    A tracker's error ends its turn, recorded in run.failed_trackers. An abort goes up to the loop, with run as it
    stood.
    """
    # Handle cover image for this tracker
    proceed, cover_url = await resolve_cover_url(
        gazelle_site,
        group_id,
        run.cover_urls,
        release.path,
        release.metadata["cover"],
        release.remove_downloaded_cover_image,
        given_url=release.given_cover_url,
    )
    if not proceed:
        # Like a failed upload: skip this tracker, and offer the next one.
        click.secho(f"\nSkipping upload to {gazelle_site.site_string}.", fg="red", bold=True)
        run.failed_trackers.append(gazelle_site.site_string)
        return Turn.NO_COVER

    if not run.spectrals_after:
        # Uploaded to this tracker's specs host, unless a tracker using the same host already did.
        run.spectral_urls = await run.spectral_uploads.urls_for(gazelle_site.site_code, run.spectral_ids)

    try:
        if not release.flac_url and not run.request_id and cfg.upload.requests.check_requests:
            run.request_id = await check_requests(gazelle_site, release.searchstrs)

        held: set[str] = set()
        if release.flac_url and release.source_flac is not None:
            click.secho(f"\nNot uploading the FLAC: transcoding from {release.flac_url}", fg="yellow")
            url = release.flac_url
            held = _held_conversions(
                release.flac_group or {},
                release.metadata,
                release.source_flac,
                release.rls_data,
                release.track_data,
            )
        else:
            group_link = f"{gazelle_site.base_url}/torrents.php?id={group_id}" if group_id else release.source_url
            torrent_id, group_id, torrent_path, torrent_content, url = await upload_and_report(
                gazelle_site,
                release.path,
                group_id,
                release.metadata,
                cover_url,
                release.track_data,
                release.hybrid,
                run.lossy_master,
                run.spectral_urls,
                run.spectral_ids,
                run.lossy_comment,
                run.request_id,
                release.source_url,
                run.seedbox_uploader,
                source=release.source,
                conversion_note=converted_from_note(release.conversion, group_link),
            )

            run.request_id = None
            run.uploaded.append(url)

            if not group_id:
                # Conversions sent with no group id would each start a new group beside this one.
                click.secho(f"\nNo group id came back for {url}: no conversions are offered for it.", fg="yellow")
                if _applicable_conversions(
                    get_downconversion_options(release.rls_data, release.track_data, quiet=True),
                    release.conversions,
                ):
                    run.failed_trackers.append(gazelle_site.site_string)
            elif not dryrun.active():
                group = await print_torrents(gazelle_site, group_id, highlight_torrent_id=torrent_id)
                # Read from the group just printed: no request of its own.
                held = _held_conversions(
                    group,
                    release.metadata,
                    {"id": torrent_id},
                    release.rls_data,
                    release.track_data,
                    uploaded=True,
                )

            if run.spectrals_after:
                # Once, on the first torrent up, whether or not the run goes on to another tracker.
                # Its transcodes, and the later trackers' uploads, then carry what it found.
                run.spectrals_after = False
                (
                    run.lossy_master,
                    run.lossy_comment,
                    run.spectral_urls,
                    run.spectral_ids,
                ) = await post_upload_spectral_check(
                    gazelle_site,
                    release.path,
                    torrent_id,
                    None,
                    release.track_data,
                    release.source,
                    release.source_url,
                    format=release.rls_data["format"],
                    uploads=run.spectral_uploads,
                )

        # Nothing to convert (an MP3 upload) asks nothing, nor does an upload whose group is not known.
        conversion_options = (
            get_downconversion_options(release.rls_data, release.track_data, quiet=True) if group_id else []
        )
        if conversion_options:
            selected_tasks = await choose_conversions(
                release.rls_data,
                release.track_data,
                conversion_options,
                held,
                release.conversions,
                ask_first=not release.flac_url,
            )
            if selected_tasks:
                display_names = [task["name"] for task in selected_tasks]
                click.secho(
                    f"\nSelected formats for downconversion: {', '.join(display_names)}",
                    fg="green",
                    bold=True,
                )

                # Execute downconversion tasks
                before = len(run.uploaded)
                await execute_downconversion_tasks(
                    selected_tasks,
                    release.path,
                    gazelle_site,
                    group_id,
                    release.metadata,
                    cover_url,
                    release.track_data,
                    release.hybrid,
                    run.lossy_master,
                    run.spectral_urls,
                    run.spectral_ids,
                    run.lossy_comment,
                    run.request_id,
                    release.source_url,
                    run.seedbox_uploader,
                    release.source,
                    url,
                    uploaded=run.uploaded,
                    max_path_length=release.max_path_length,
                )
                if len(run.uploaded) - before < len(selected_tasks):
                    run.failed_trackers.append(gazelle_site.site_string)
    except RequestError as e:
        click.secho(f"\nUpload to {gazelle_site.site_string} failed: {e}", fg="red", bold=True)
        print_certificate_hint(e)
        run.failed_trackers.append(gazelle_site.site_string)
        if isinstance(e, UnknownOutcomeError):
            record.note_unknown_outcome(e)

    return Turn.ENDED
