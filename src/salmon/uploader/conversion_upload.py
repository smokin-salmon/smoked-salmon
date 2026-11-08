"""Making and uploading the conversions an upload chose, each from the album folder."""

from typing import TYPE_CHECKING, Any

import anyio
import asyncclick as click
from mutagen import MutagenError

import salmon.trackers
from salmon import cfg, dryrun
from salmon.converter.conversions import conversion_of, recorded_files
from salmon.converter.downconverting import convert_folder, generate_conversion_description
from salmon.converter.transcoding import generate_transcode_description, transcode_folder
from salmon.errors import UploadError
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.folderstructure import check_folder_structure
from salmon.trackers.base import TagRules
from salmon.uploader.conversion_choice import downconversion_format
from salmon.uploader.seedbox import UploadManager
from salmon.uploader.upload import upload_and_report

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


def _max_path_length_for_run(gazelle_site: "BaseGazelleApi", trackers: list[str] | None = None) -> int:
    """The path limit to check the folder against: this tracker's, or the strictest among every
    tracker configured, when the run might go on to upload to another of them afterward.

    Which trackers a run actually reaches is only known one at a time: after each upload, the
    user is offered a choice among the remaining configured trackers. The folder is checked once,
    before the first upload, so it is checked against the strictest limit already knowable then.
    Trackers named with -t are known up front: the strictest of theirs.
    """
    own_limit = getattr(gazelle_site, "TAG_RULES", TagRules()).max_path_length
    if not trackers and not cfg.upload.multi_tracker_upload:
        return own_limit
    limits = [own_limit]
    for code in trackers or salmon.trackers.tracker_list:
        tracker_class = salmon.trackers.tracker_classes.get(code)
        if tracker_class is not None:
            limits.append(tracker_class.TAG_RULES.max_path_length)
    return min(limits)


def _by_source_name(folder: str, audio_info: dict[str, Any]) -> dict[str, Any]:
    """A converted folder's audio info, by the names its files had in the source, as salmon recorded them.

    The folder check may have shortened them since, on an earlier tracker's turn. Without a record listing exactly
    these files, the info stays by the names they have.
    """
    record = conversion_of(folder)
    files = None if record is None else recorded_files(record)
    if files is None or set(files.values()) != audio_info.keys() or len(set(files.values())) != len(files):
        return audio_info
    return {source: audio_info[path] for source, path in files.items()}


async def execute_downconversion_tasks(
    selected_tasks: list[dict[str, Any]],
    path: str,
    gazelle_site: "BaseGazelleApi",
    group_id: int | None,
    metadata: dict[str, Any],
    cover_url: str | None,
    track_data: dict[str, Any],
    hybrid: bool,
    lossy_master: bool,
    spectral_urls: dict[int, list[str]] | None,
    spectral_ids: dict[int, str] | None,
    lossy_comment: str | None,
    request_id: int | str | None,
    source_url: str | None,
    seedbox_uploader: UploadManager,
    source: str | None,
    base_url: str,
    *,
    uploaded: list[str] | None = None,
    max_path_length: int | None = None,
) -> None:
    """Execute the selected downconversion tasks.

    Args:
        selected_tasks: List of downconversion task dicts.
        path: Path to the album folder.
        gazelle_site: The tracker API instance.
        group_id: Optional existing group ID.
        metadata: Release metadata.
        cover_url: Cover image URL.
        track_data: Track information.
        hybrid: Whether this is a hybrid release.
        lossy_master: Whether this is lossy mastered.
        spectral_urls: Spectral image URLs.
        spectral_ids: Spectral IDs.
        lossy_comment: Lossy approval comment.
        request_id: Request ID to fill.
        source_url: Source URL.
        seedbox_uploader: Seedbox upload manager.
        source: Media source.
        base_url: Base URL for the original upload.
        uploaded: Where to add the URL of each torrent uploaded.
        max_path_length: The longest in-torrent path each conversion may have: the run's limit, the one its album
            folder was checked against. None: gazelle_site's run limit, without the trackers named with -t.
    """
    if uploaded is None:
        uploaded = []
    if max_path_length is None:
        max_path_length = _max_path_length_for_run(gazelle_site)

    base_path = path
    # A dry run's go into its scratch directory, removed with it.
    output_dir = dryrun.scratch_dir() if dryrun.active() else cfg.directory.download_directory

    override_lossy_comment = (
        f"Transcode of {base_url}\n[hide=Lossy comment of original torrent]{lossy_comment}[/hide]\n"
        if lossy_comment
        else None
    )

    for task in selected_tasks:
        click.secho(f"\nProcessing: {task['name']}", fg="cyan", bold=True)

        if task["action"] == "downconvert":
            # Execute downconversion
            try:
                sample_rate, new_path = await convert_folder(
                    base_path,
                    bit_depth=task["target_bitdepth"],
                    sample_rate=task["target_sample_rate"],
                    output_dir=output_dir,
                )
            except (UploadError, OSError) as e:
                click.secho(f"  {e}", fg="red", bold=True)
                continue
            await anyio.sleep(0.1)

            # The upload describes the converted files (their sample rate, for one), not the source's.
            # A folder that was already there may hold other files: then it is not this conversion.
            try:
                converted_info = _by_source_name(new_path, gather_audio_info(new_path))
            except (UploadError, MutagenError) as e:
                click.secho(f"  Could not read {new_path} ({e}): not uploading it.", fg="red", bold=True)
                continue
            if converted_info.keys() != track_data.keys():
                click.secho(
                    f"  {new_path} does not hold the same audio files as the source: not uploading it.",
                    fg="red",
                    bold=True,
                )
                continue
            # Nor is it this conversion if its files are not in the format the task makes.
            expected = (task["target_bitdepth"], task["target_sample_rate"])
            found = sorted({(info["precision"], info["sample rate"]) for info in converted_info.values()})
            if found != [expected]:
                found_formats = ", ".join(f"{bits} bit {rate / 1000:g} kHz" for bits, rate in found)
                click.secho(
                    f"  {new_path} holds {found_formats} files, not {expected[0]} bit {expected[1] / 1000:g} kHz: "
                    "not uploading it.",
                    fg="red",
                    bold=True,
                )
                continue
            conversion_track_data = {name: {**track, **converted_info[name]} for name, track in track_data.items()}

            # Update metadata for this conversion
            conversion_metadata = metadata.copy()
            conversion_metadata["format"], conversion_metadata["encoding"] = downconversion_format(task)

            # Generate description for conversion
            description = generate_conversion_description(base_url, sample_rate, task["target_bitdepth"])
            click.secho(f"  Generated description: {description[:100]}...", fg="blue")
            await check_folder_structure(new_path, conversion_metadata["scene"], max_path_length=max_path_length)

            # Upload the converted version
            torrent_id, group_id, torrent_path, torrent_content, new_url = await upload_and_report(
                gazelle_site,
                new_path,
                group_id,
                conversion_metadata,
                cover_url,
                conversion_track_data,
                hybrid,
                lossy_master,
                spectral_urls,
                spectral_ids,
                lossy_comment,
                request_id,
                source_url,
                seedbox_uploader,
                source=source,
                override_description=description,
                override_lossy_comment=override_lossy_comment,
            )
            uploaded.append(new_url)

            click.secho(f"  ✓ {task['name']} conversion completed", fg="green")

        elif task["action"] == "transcode":
            # Call transcode function
            click.secho(f"  Target encoding: {task['encoding']}", fg="white")

            # Execute transcoding
            try:
                transcoded_path = await transcode_folder(base_path, task["encoding"], output_dir=output_dir)
            except (UploadError, OSError) as e:
                click.secho(f"  {e}", fg="red", bold=True)
                continue
            await anyio.sleep(0.1)

            # Update metadata for this transcode
            transcode_metadata = metadata.copy()
            transcode_metadata["format"], transcode_metadata["encoding"] = downconversion_format(task)
            transcode_metadata["encoding_vbr"] = {"320": False, "V0": True}[task["encoding"]]

            # Generate description for transcode
            description = generate_transcode_description(base_url, task["encoding"])
            click.secho(f"  Generated description: {description[:100]}...", fg="blue")
            await check_folder_structure(transcoded_path, transcode_metadata["scene"], max_path_length=max_path_length)

            # Upload the transcoded version
            torrent_id, group_id, torrent_path, torrent_content, new_url = await upload_and_report(
                gazelle_site,
                transcoded_path,
                group_id,
                transcode_metadata,
                cover_url,
                track_data,
                hybrid,
                lossy_master,
                spectral_urls,
                spectral_ids,
                lossy_comment,
                request_id,
                source_url,
                seedbox_uploader,
                source=source,
                override_description=description,
                override_lossy_comment=override_lossy_comment,
            )
            uploaded.append(new_url)

            click.secho(f"  ✓ {task['name']} transcode completed", fg="green")
