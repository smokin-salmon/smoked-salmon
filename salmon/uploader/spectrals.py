import contextlib
import os
import platform
import random
import re
import shutil
import time
from functools import partial
from os.path import dirname, join
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
import asyncclick as click
import oxipng

from salmon import cfg
from salmon.common import flush_stdin, get_audio_files, prompt_async
from salmon.common.files import process_files
from salmon.errors import (
    AbortAndDeleteFolder,
    ImageUploadFailed,
    UploadError,
    WebServerIsAlreadyRunning,
)
from salmon.images import upload_spectrals as upload_spectral_imgs
from salmon.web import create_app_async, spectrals

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


THREADS = [None] * cfg.upload.simultaneous_threads


async def check_spectrals(
    path: str,
    audio_info: dict[str, Any],
    lossy_master: bool | None = None,
    spectral_ids: tuple[int, ...] | dict[int, str] | None = None,
    check_lma: bool = True,
    force_prompt_lossy_master: bool = False,
    format: str = "FLAC",
) -> tuple[bool | None, dict[int, str] | None]:
    """Run spectral checker functions.

    Generate spectrals and ask whether files are lossy. If IDs were not provided,
    prompt for spectrals to upload.

    Args:
        path: Path to the album folder.
        audio_info: Audio file information dict.
        lossy_master: Whether files are lossy mastered.
        spectral_ids: Track IDs for spectrals.
        check_lma: Whether to check for lossy master.
        force_prompt_lossy_master: Force lossy master prompt.
        format: Audio format.

    Returns:
        Tuple of (lossy_master, spectral_ids).
    """
    if format != "FLAC":
        click.secho(
            "Spectrals are only generated for FLAC files. Skipping...",
            fg="cyan",
        )
        return None, None
    click.secho("\nChecking lossy master / spectrals...", fg="cyan", bold=True)
    spectrals_path = create_specs_folder(path)
    all_spectral_ids: dict[int, str] = {}
    if not spectral_ids:
        all_spectral_ids = await generate_spectrals_all(path, spectrals_path, audio_info)
        while True:
            await view_spectrals(spectrals_path, all_spectral_ids)
            if lossy_master is None and check_lma:
                lossy_master = await prompt_lossy_master(force_prompt_lossy_master)
                if lossy_master is not None:
                    break
            else:
                break
    else:
        if lossy_master is None:
            lossy_master = await prompt_lossy_master(force_prompt_lossy_master)

    if not spectral_ids:
        spectral_ids = await prompt_spectrals(
            all_spectral_ids,
            lossy_master,
            check_lma,
            force_prompt_lossy_master=force_prompt_lossy_master,
        )
    else:
        spectral_ids = await generate_spectrals_ids(path, spectral_ids, spectrals_path, audio_info)

    return lossy_master, spectral_ids


async def handle_spectrals_upload_and_deletion(
    spectrals_path: str,
    spectral_ids: dict[int, str] | None,
    delete_spectrals: bool = True,
) -> dict[int, list[str]] | None:
    """Upload spectrals and optionally delete local files.

    Args:
        spectrals_path: Path to spectrals folder.
        spectral_ids: Dict mapping spectral IDs to filenames.
        delete_spectrals: Whether to delete local spectral files.

    Returns:
        Dict mapping spectral IDs to uploaded URLs.
    """
    spectral_urls = await upload_spectrals(spectrals_path, spectral_ids)
    if delete_spectrals and os.path.isdir(spectrals_path):
        shutil.rmtree(spectrals_path, ignore_errors=True)
        time.sleep(0.5)
        if os.path.isdir(spectrals_path):
            shutil.rmtree(spectrals_path)
            time.sleep(0.5)
    return spectral_urls


async def generate_spectrals_all(path: str, spectrals_path: str, audio_info: dict[str, Any]) -> dict[int, str]:
    """Generate spectral images for all audio files.

    Args:
        path: Path to the album directory.
        spectrals_path: Path to the spectrals output folder.
        audio_info: Audio file information dict.

    Returns:
        Dictionary mapping track numbers to filenames.
    """
    files_li = get_audio_files(path, True)
    return await _generate_spectrals(path, files_li, spectrals_path, audio_info)


async def generate_spectrals_ids(
    path: str,
    track_ids: tuple[int, ...] | dict[int, str],
    spectrals_path: str,
    audio_info: dict[str, Any],
) -> dict[int, str]:
    """Generate spectral images for specific track IDs.

    Args:
        path: Path to the album directory.
        track_ids: Tuple of 1-based track IDs to generate spectrals for.
        spectrals_path: Path to the spectrals output folder.
        audio_info: Audio file information dict.

    Returns:
        Dictionary mapping track numbers to filenames.
    """
    if track_ids == (0,):
        click.secho("Uploading no spectrals...", fg="yellow")
        return {}

    wanted_filenames = get_wanted_filenames(list(audio_info), track_ids)
    files_li = [fn for fn in get_audio_files(path) if fn in wanted_filenames]
    return await _generate_spectrals(path, files_li, spectrals_path, audio_info)


def get_wanted_filenames(filenames, track_ids):
    """Get the filenames from the spectrals specified as cli options."""
    try:
        return {filenames[i - 1] for i in track_ids}
    except IndexError:
        raise UploadError("Spectral IDs out of range.") from None


async def _generate_spectral_for_file(
    path: str, filename: str, spectrals_path: str, audio_info: dict[str, Any], idx: int
) -> tuple[int, str]:
    """Generate full and zoomed spectral images for a single audio file.

    Args:
        path: Path to the album directory.
        filename: Relative filename of the audio file.
        spectrals_path: Path to the spectrals output folder.
        audio_info: Audio file information dict.
        idx: Zero-based index of the file.

    Returns:
        Tuple of (1-based track number, filename).
    """
    zoom_startpoint = calculate_zoom_startpoint(audio_info[filename])

    full_spectral_path = os.path.join(spectrals_path, f"{idx + 1:02d} Full.png")
    zoom_spectral_path = os.path.join(spectrals_path, f"{idx + 1:02d} Zoom.png")

    # Run the process for generating the spectrals
    await anyio.run_process(
        [
            "sox",
            "--multi-threaded",
            os.path.join(path, filename),
            "--buffer",
            "128000",
            "-n",
            "remix",
            "1",
            "spectrogram",
            "-x",
            "2000",
            "-y",
            "513",
            "-z",
            "120",
            "-w",
            "Kaiser",
            "-o",
            full_spectral_path,
            "remix",
            "1",
            "spectrogram",
            "-x",
            "500",
            "-y",
            "1025",
            "-z",
            "120",
            "-w",
            "Kaiser",
            "-S",
            str(zoom_startpoint),
            "-d",
            "0:02",
            "-o",
            zoom_spectral_path,
        ],
        check=True,  # Raise error if process fails
    )

    return (idx + 1, filename)  # Return the filename to track progress


async def _generate_spectrals(
    path: str, files_li: list[str], spectrals_path: str, audio_info: dict[str, Any]
) -> dict[int, str]:
    """Generate spectral images for a list of audio files.

    Args:
        path: Path to the album directory.
        files_li: List of relative audio filenames.
        spectrals_path: Path to the spectrals output folder.
        audio_info: Audio file information dict.

    Returns:
        Sorted dictionary mapping track numbers to filenames.
    """
    spectral_ids: dict[int, str] = {}

    results = await process_files(
        files_li,
        lambda file, idx: _generate_spectral_for_file(path, file, spectrals_path, audio_info, idx),
        "Generating Spectrals",
    )

    click.secho("Finished generating spectrals.", fg="green")
    if cfg.upload.compression.compress_spectrals:
        await _compress_spectrals(spectrals_path)

    for result in results:
        if result:
            track_num, filename = result
            spectral_ids[track_num] = filename

    sorted_spectrals = dict(sorted(spectral_ids.items()))

    return sorted_spectrals


async def _compress_single_spectral(filepath: str, _idx: int) -> None:
    """Compress a single spectral PNG image using oxipng in a thread.

    Args:
        filepath: Path to the PNG file to compress.
        _idx: Unused index parameter for process_files compatibility.
    """
    func = partial(oxipng.optimize, filepath, level=2, strip=oxipng.StripChunks.all())
    return await anyio.to_thread.run_sync(func)


async def _compress_spectrals(spectrals_path: str) -> None:
    """Compress all spectral PNG images in a directory using oxipng.

    Args:
        spectrals_path: Path to the directory containing spectral PNG files.
    """
    files = [f for f in os.listdir(spectrals_path) if f.endswith(".png")]
    if not files:
        return

    filepaths = [os.path.join(spectrals_path, f) for f in files]

    await process_files(
        filepaths,
        _compress_single_spectral,
        "Compressing spectral images",
    )

    click.secho("Finished compressing spectrals.", fg="green")


def get_spectrals_path(path):
    """Get the path to the spectrals folder for an album."""
    if cfg.directory.tmp_dir and os.path.isdir(cfg.directory.tmp_dir):
        # Create a unique subfolder for this album
        base_name = os.path.basename(path.rstrip("/"))
        return os.path.join(cfg.directory.tmp_dir, f"spectrals_{base_name}")
    return os.path.join(path, "Spectrals")


def create_specs_folder(path):
    """Create the spectrals folder."""
    spectrals_path = get_spectrals_path(path)
    if os.path.isdir(spectrals_path):
        shutil.rmtree(spectrals_path)
    os.mkdir(spectrals_path)
    return spectrals_path


def calculate_zoom_startpoint(track_data):
    """
    Calculate the point in the track to generate the zoom. Do 5 seconds before
    the end of the track if it's over 5 seconds long. Otherwise start at 2.
    """
    if "duration" in track_data and track_data["duration"] > 5:
        return track_data["duration"] // 2
    return 0


async def view_spectrals(spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
    """Open the generated spectrals in an image viewer.

    Args:
        spectrals_path: Path to spectrals folder.
        all_spectral_ids: Dict mapping spectral IDs to filenames.
    """
    if not cfg.upload.native_spectrals_viewer:
        await _open_specs_in_web_server(spectrals_path, all_spectral_ids)
    elif platform.system() == "Darwin":
        await _open_specs_in_preview(spectrals_path)
    elif platform.system() == "Windows":
        _open_specs_in_windows(spectrals_path)
    else:
        await _open_specs_in_feh(spectrals_path)


async def _open_specs_in_preview(spectrals_path: str) -> None:
    """Open spectral images in macOS Quick Look preview.

    Args:
        spectrals_path: Path to the spectrals directory.
    """
    files = sorted(Path(spectrals_path).glob("*"))
    if not files:
        return
    args = ["qlmanage", "-p", *(str(f) for f in files)]
    await anyio.run_process(args, check=False)


async def _open_specs_in_feh(spectrals_path: str) -> None:
    """Open spectral images in feh image viewer on Linux.

    Args:
        spectrals_path: Path to the spectrals directory.
    """
    args = [
        "feh",
        "--cycle-once",
        "--sort",
        "filename",
        "-d",
        "--auto-zoom",
        "-geometry",
        "-.",
        spectrals_path,
    ]
    if cfg.upload.feh_fullscreen:
        args.insert(4, "--fullscreen")
    await anyio.run_process(args, check=False)


def _open_specs_in_windows(spectrals_path):
    png_files = [os.path.join(spectrals_path, f) for f in os.listdir(spectrals_path) if f.lower().endswith(".png")]

    if not png_files:
        click.secho("No PNG files found to display.", fg="yellow")
        return
    png_files.sort()
    os.startfile(png_files[0])


async def _open_specs_in_web_server(specs_path, all_spectral_ids):
    spectrals.set_active_spectrals(all_spectral_ids)
    symlink_path = join(dirname(dirname(__file__)), "web", "static", "specs")

    runner = None
    try:
        try:
            os.symlink(specs_path, symlink_path)
        except FileExistsError:
            os.unlink(symlink_path)
            os.symlink(specs_path, symlink_path)
        with contextlib.suppress(WebServerIsAlreadyRunning):
            runner = await create_app_async()
        url = f"http://{cfg.upload.web_interface.host}:{cfg.upload.web_interface.port}/spectrals"
        await prompt_async(
            click.style(
                f"\nSpectrals are available at {click.style(url, fg='blue', underline=True)}\n"
                f"""{
                    click.style(
                        "Press enter once you are finished viewing to continue the uploading process",
                        fg="magenta",
                        bold=True,
                    )
                }""",
                fg="magenta",
            ),
            end=" ",
            flush=True,
        )
        if runner is not None:
            await runner.cleanup()
    finally:
        os.unlink(symlink_path)


async def upload_spectrals(
    spectrals_path: str,
    spectral_ids: dict[int, str] | None,
) -> dict[int, list[str]] | None:
    """Upload spectral images to image host.

    Args:
        spectrals_path: Path to spectrals folder.
        spectral_ids: Dict mapping spectral IDs to filenames.

    Returns:
        Dict mapping spectral IDs to uploaded URLs, or None.
    """
    if not spectral_ids:
        return None

    spectrals_list: list[tuple[int, str, tuple[str, str]]] = []
    for sid, filename in spectral_ids.items():
        spectrals_list.append(
            (
                sid,
                filename,
                (
                    os.path.join(spectrals_path, f"{sid:02d} Full.png"),
                    os.path.join(spectrals_path, f"{sid:02d} Zoom.png"),
                ),
            )
        )

    try:
        return await upload_spectral_imgs(spectrals_list)
    except ImageUploadFailed as e:
        click.secho(f"Failed to upload spectral: {e}", fg="red")
        return None


async def prompt_spectrals(spectral_ids, lossy_master, check_lma, force_prompt_lossy_master=False):
    """Ask which spectral IDs the user wants to upload."""
    while True:
        ids = (
            "*"
            if cfg.upload.yes_all and not force_prompt_lossy_master
            else await click.prompt(
                click.style(
                    f"What spectral IDs would you like to upload to {cfg.image.specs_uploader}? "
                    '(space-separated list of IDs, "0" for none, "*" for all, or "+" for a randomized selection)',
                    fg="magenta",
                ),
                default="*" if lossy_master else "+",
            )
        )
        if ids.strip() == "+":
            all_ids = list(spectral_ids.keys())
            subset_size = max(1, len(all_ids) // 3)  # Ensure at least one ID is selected
            ids = sorted([str(i) for i in random.sample(all_ids, subset_size)], key=int)
            return {int(id_): spectral_ids[int(id_)] for id_ in ids}
        if ids.strip() == "*":
            return spectral_ids
        elif ids.strip() == "0":
            return None
        ids = [i.strip() for i in ids.split()]
        if not ids and lossy_master and check_lma:
            click.secho(
                "This release has been flagged as lossy master, please select at least one spectral.",
                fg="red",
            )
            continue
        if all(i.isdigit() and int(i) in spectral_ids for i in ids):
            return {int(id_): spectral_ids[int(id_)] for id_ in ids}
        click.secho(
            f"Invalid IDs. Valid IDs are: {', '.join(str(s) for s in spectral_ids)}.",
            fg="red",
        )


async def prompt_lossy_master(force_prompt_lossy_master=False):
    while True:
        flush_stdin()
        r = (
            "n"
            if cfg.upload.yes_all and not force_prompt_lossy_master
            else (
                await click.prompt(
                    click.style(
                        "\nIs this release lossy mastered? "
                        "[y]es, [N]o, [r]eopen spectrals, [a]bort, [d]elete music folder",
                        fg="magenta",
                    ),
                    type=click.STRING,
                    default="n",
                )
            )[0].lower()
        )
        if r == "y":
            return True
        elif r == "n":
            return False
        elif r == "r":
            return None
        elif r == "a":
            raise click.Abort
        elif r == "d":
            raise AbortAndDeleteFolder


async def report_lossy_master(
    gazelle_site: "BaseGazelleApi",
    torrent_id: int,
    spectral_urls: dict[int, list[str]] | None,
    spectral_ids: dict[int, str] | None,
    source: str | None,
    comment: str | None,
    source_url: str | None = None,
) -> None:
    """Report torrent for lossy WEB/master approval.

    Args:
        gazelle_site: The tracker API instance.
        torrent_id: The torrent ID.
        spectral_urls: Spectral image URLs.
        spectral_ids: Spectral IDs.
        source: Media source.
        comment: Lossy approval comment.
        source_url: Source URL.
    """
    comment = _add_spectral_links_to_lossy_comment(comment, source_url, spectral_urls, spectral_ids)
    if source is None:
        click.secho("Cannot report lossy master without source.", fg="red")
        return
    await gazelle_site.report_lossy_master(torrent_id, comment, source)
    click.secho("\nReported upload for Lossy Master/WEB Approval Request.", fg="cyan")


async def generate_lossy_approval_comment(source_url, filenames, force_prompt_lossy_master=False):
    while True:
        comment = (
            ""
            if cfg.upload.yes_all and not force_prompt_lossy_master
            else await click.prompt(
                click.style(
                    "Do you have a comment for the lossy approval report? It is appropriate to "
                    "make a note about the source here. Source information from go, gos, and the "
                    "queue will be included automatically.",
                    fg="cyan",
                    bold=True,
                ),
                default="",
            )
        )
        if comment or source_url:
            return comment
        click.secho(
            "This release was not uploaded with go, gos, or the queue, so you must add a comment about the source.",
            fg="red",
        )


def _add_spectral_links_to_lossy_comment(comment, source_url, spectral_urls, spectral_ids):
    if comment:
        comment += "\n\n"
    if source_url:
        comment += f"Sourced from: {source_url}\n\n"
    comment += make_spectral_bbcode(spectral_ids, spectral_urls)
    return comment


def make_spectral_bbcode(spectral_ids, spectral_urls):
    "Generates the bbcode for spectrals in descriptions and reports."
    if not spectral_urls:
        return ""
    bbcode = "[hide=Spectrals]"
    for spec_id, urls in spectral_urls.items():
        filename = re.sub(r"[\[\]]", "_", spectral_ids[spec_id])
        bbcode += f"[b]{filename} Full[/b]\n[img={urls[0]}]\n[hide=Zoomed][img={urls[1]}][/hide]\n\n"
    bbcode += "[/hide]\n"
    return bbcode


async def post_upload_spectral_check(
    gazelle_site: "BaseGazelleApi",
    path: str,
    torrent_id: int,
    spectral_ids: dict[int, str] | None,
    track_data: dict[str, Any],
    source: str | None,
    source_url: str | None,
    format: str = "FLAC",
) -> tuple[bool | None, str | None, dict[int, list[str]] | None, dict[int, str] | None]:
    """Generate and add spectrals after upload.

    As this is post upload, we have time to ask if this is a lossy master.

    Args:
        gazelle_site: The tracker API instance.
        path: Path to the album folder.
        torrent_id: The torrent ID.
        spectral_ids: Spectral IDs.
        track_data: Track information.
        source: Media source.
        source_url: Source URL.
        format: Audio format.

    Returns:
        Tuple of (lossy_master, lossy_comment, spectral_urls, spectral_ids).
    """
    lossy_master, spectral_ids = await check_spectrals(
        path, track_data, None, spectral_ids, force_prompt_lossy_master=True, format=format
    )
    if not lossy_master and not spectral_ids:
        return None, None, None, None

    lossy_comment = None
    if lossy_master:
        lossy_comment = await generate_lossy_approval_comment(
            source_url, list(track_data.keys()), force_prompt_lossy_master=True
        )
        click.echo()

    spectrals_path = get_spectrals_path(path)
    spectral_urls = await handle_spectrals_upload_and_deletion(spectrals_path, spectral_ids)

    if spectral_urls:
        spectrals_bbcode = make_spectral_bbcode(spectral_ids, spectral_urls)
        await gazelle_site.append_to_torrent_description(torrent_id, spectrals_bbcode)

    if lossy_master:
        await report_lossy_master(
            gazelle_site,
            torrent_id,
            spectral_urls,
            spectral_ids,
            source,
            lossy_comment,
            source_url,
        )
    return lossy_master, lossy_comment, spectral_urls, spectral_ids
