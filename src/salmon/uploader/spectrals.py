import errno
import os
import platform
import random
import re
import shutil
import tempfile
import textwrap
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from subprocess import DEVNULL
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
import asyncclick as click

# pyoxipng 9.1.1 has wheels up to CPython 3.13 only, and building it needs Rust (and MSVC on Windows), so
# pyproject.toml installs it below 3.14 only and spectrals are left uncompressed on newer Pythons (re-saving them with
# Pillow made them bigger). Drop the marker and this fallback once pyoxipng ships 3.14 or abi3 wheels.
try:
    import oxipng
except ImportError:
    oxipng = None

from salmon import cfg, dryrun
from salmon.common import flush_stdin, get_audio_files, prompt_async
from salmon.common.files import process_files
from salmon.errors import (
    AbortAndDeleteFolder,
    ImageUploadFailed,
    RequestError,
    UnknownOutcomeError,
    UploadError,
)
from salmon.images import upload_spectrals as upload_spectral_imgs
from salmon.web import create_app_async, spectrals

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


async def check_spectrals(
    path: str,
    audio_info: dict[str, Any],
    lossy_master: bool | None = None,
    spectral_ids: tuple[int, ...] | dict[int, str] | None = None,
    check_lma: bool = True,
    force_prompt_lossy_master: bool = False,
    format: str = "FLAC",
    hosts: str | None = None,
    offer_deletion: bool = True,
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
        hosts: Where the spectrals go, as the prompt names them: see specs_hosts_text. Defaults to
            the shared specs_uploader.
        offer_deletion: Whether the lossy master prompt offers to delete the music folder.

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
        # Printed whether or not the question follows: salmon specs prints it and asks nothing.
        marks_found = False
        if lossy_master is None:
            marks_found = await print_frequency_analysis(path, spectrals_path, all_spectral_ids)
        while True:
            await view_spectrals(spectrals_path, all_spectral_ids)
            if lossy_master is None and check_lma:
                lossy_master = await prompt_lossy_master(force_prompt_lossy_master, offer_deletion, marks_found)
                if lossy_master is not None:
                    break
            else:
                break
        spectral_ids = await prompt_spectrals(
            all_spectral_ids,
            lossy_master,
            check_lma,
            force_prompt_lossy_master=force_prompt_lossy_master,
            hosts=hosts,
        )
        if spectral_ids and cfg.upload.compression.compress_spectrals:
            await _compress_spectrals(spectrals_path, spectral_ids)
    else:
        # Before the plots are written: this compresses every image in the folder.
        spectral_ids = await generate_spectrals_ids(path, spectral_ids, spectrals_path, audio_info)
        if lossy_master is None:
            marks_found = await print_frequency_analysis(path, spectrals_path, spectral_ids)
            lossy_master = await prompt_lossy_master(force_prompt_lossy_master, offer_deletion, marks_found)

    return lossy_master, spectral_ids


_FREQUENCY_HEADINGS = {
    "suspect": ("the marks of a lossy encoder", "red"),
    "look": ("one mark of a lossy encoder, not both", "yellow"),
    "ok": ("no mark of a lossy encoder", "green"),
    "none": ("nothing could be measured", "yellow"),
}


def spectrum_plot_name(spectral_id: int) -> str:
    """The name of a track's averaged-spectrum plot in the spectrals folder.

    It sorts between the track's "NN Full.png" and "NN Zoom.png", so a viewer that opens a folder's first image
    still opens a spectral.
    """
    return f"{spectral_id:02d} Spectrum.png"


async def print_frequency_analysis(path: str, spectrals_path: str, spectral_ids: dict[int, str]) -> bool:
    """Measure the marks a lossy encoder leaves in each track and print them.

    Printed before the lossy-master question, or alone where none is asked (salmon specs).

    Each track with spectrals gets its averaged-spectrum plot next to them, under the same spectral ID: see
    spectrum_plot_name. Never raises: a failed analysis prints one line.

    Args:
        path: Path to the album folder.
        spectrals_path: Path to the spectrals folder.
        spectral_ids: The spectrals in that folder, by spectral ID.

    Returns:
        Whether a track carries the marks, which makes "yes" the question's default.
    """
    try:
        # numpy, PyAV and Pillow: loaded when the analysis runs, not when salmon starts.
        from salmon.uploader import frequency

        plot_paths = {
            filename: os.path.join(spectrals_path, spectrum_plot_name(sid)) for sid, filename in spectral_ids.items()
        }
        results = await frequency.generate_frequency_plots(path, get_audio_files(path, True), plot_paths)
        level, notes = frequency.assess(results)
    except Exception as e:
        click.secho(f"\nFrequency analysis failed, so it says nothing about this release: {e!r}", fg="yellow")
        return False
    heading, colour = _FREQUENCY_HEADINGS[level]
    click.secho(f"\nFrequency analysis: {heading}", fg=colour, bold=True)
    for note in notes:
        click.echo(f"  {note}")
    if any(result.image for result in results):
        click.echo(f'  Averaged spectra: "NN Spectrum.png", next to the spectrals in {spectrals_path}')
    click.secho("  A measurement, not a verdict: read the spectrals before answering.", fg="cyan")
    return level == "suspect"


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
    if delete_spectrals:
        await _delete_spectrals(spectrals_path)
    return spectral_urls


async def _delete_spectrals(spectrals_path: str) -> None:
    """Delete a spectrals folder, trying twice: on Windows, a viewer may still hold its files."""
    if os.path.isdir(spectrals_path):
        shutil.rmtree(spectrals_path, ignore_errors=True)
        await anyio.sleep(0.5)
        if os.path.isdir(spectrals_path):
            shutil.rmtree(spectrals_path)
            await anyio.sleep(0.5)


def specs_hosts_text(trackers: Sequence[str]) -> str:
    """Name where these trackers' spectrals go: "catbox", or "catbox (RED), imgbox (OPS/DIC)" when hosts differ."""
    trackers_by_host: dict[str, list[str]] = {}
    for tracker in trackers:
        trackers_by_host.setdefault(cfg.image.host_for(tracker, "specs_uploader"), []).append(tracker)
    if len(trackers_by_host) == 1:
        return next(iter(trackers_by_host))
    return ", ".join(f"{host} ({'/'.join(codes)})" for host, codes in trackers_by_host.items())


class SpectralUploads:
    """A release's spectrals in an upload run, uploaded once per image host and reused by the trackers sharing it.

    A tracker's spectrals go to its specs host: image.<tracker>.specs_uploader, else image.specs_uploader.
    Once every tracker the run may upload to has its upload, the spectral files are deleted. Until then,
    they are kept, outside the album folder so that no torrent made from it holds them, and close()
    deletes them.
    """

    def __init__(self, album_path: str, trackers: Sequence[str]) -> None:
        """Set up the uploads for an album.

        Args:
            album_path: The album folder, as the spectrals were made from it.
            trackers: The site codes of every tracker the run may upload to.
        """
        self._album_path = album_path
        self._path = get_spectrals_path(album_path)
        self._trackers = list(trackers)
        self._hosts = {cfg.image.host_for(tracker, "specs_uploader") for tracker in self._trackers}
        self._urls: dict[str, dict[int, list[str]] | None] = {}
        self._scratch: str | None = None

    def hosts_text(self) -> str:
        """Name where the spectrals go, for prompts: see specs_hosts_text."""
        return specs_hosts_text(self._trackers)

    async def urls_for(self, tracker: str, spectral_ids: dict[int, str] | None) -> dict[int, list[str]] | None:
        """Get the spectral URLs for a tracker, uploading them to its host if no tracker of the run did yet.

        Args:
            tracker: The tracker's site code.
            spectral_ids: The spectrals to upload, by track ID.

        Returns:
            The URLs of each spectral by track ID, or None if there are none: see upload_spectrals.
        """
        host = cfg.image.host_for(tracker, "specs_uploader")
        if host not in self._urls:
            self._urls[host] = await upload_spectrals(self._path, spectral_ids, tracker)
            if not spectral_ids or self._hosts <= self._urls.keys():
                await self.close()
            else:
                self._keep_out_of_album()
        return self._urls[host]

    def _keep_out_of_album(self) -> None:
        """Move the spectrals folder out of the album folder, if it is inside it, to where close() deletes it."""
        if self._scratch is not None or not os.path.isdir(self._path):
            return
        if not Path(self._path).resolve().is_relative_to(Path(self._album_path).resolve()):
            return
        self._scratch = tempfile.mkdtemp(prefix="salmon-spectrals-")
        self._path = shutil.move(self._path, os.path.join(self._scratch, "Spectrals"))

    async def close(self) -> None:
        """Delete the spectral files, if they are still there."""
        await _delete_spectrals(self._path)
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None


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
    # Compression happens after the user selects which spectrals to upload
    # (see check_spectrals), not here, since most generated spectrals are
    # only for viewing and are never uploaded.
    return await _generate_spectrals(path, files_li, spectrals_path, audio_info, compress=False)


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
    path: str,
    files_li: list[str],
    spectrals_path: str,
    audio_info: dict[str, Any],
    compress: bool = True,
) -> dict[int, str]:
    """Generate spectral images for a list of audio files.

    Args:
        path: Path to the album directory.
        files_li: List of relative audio filenames.
        spectrals_path: Path to the spectrals output folder.
        audio_info: Audio file information dict.
        compress: Whether to compress the generated spectrals immediately.
            Set to False when the caller will compress only a subset later
            (e.g. once the user has picked which spectrals to upload).

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
    if compress and cfg.upload.compression.compress_spectrals:
        await _compress_spectrals(spectrals_path)

    for result in results:
        if result:
            track_num, filename = result
            spectral_ids[track_num] = filename

    sorted_spectrals = dict(sorted(spectral_ids.items()))

    return sorted_spectrals


_not_compressed_notice_shown = False


def _notify_spectrals_not_compressed() -> None:
    """Say, once per run, that spectrals go up uncompressed because oxipng is not installed."""
    global _not_compressed_notice_shown
    if _not_compressed_notice_shown:
        return
    _not_compressed_notice_shown = True
    click.secho(
        "Spectrals are not compressed: oxipng is not available for this Python version. Installing salmon with "
        '"uv tool install --python 3.13 git+https://github.com/smokin-salmon/smoked-salmon" compresses them, '
        "and so does installing the oxipng program (winget, scoop, brew, apt, or its GitHub releases).",
        fg="yellow",
    )


OXIPNG_PROGRAM_TIMEOUT = 60


async def _compress_with_oxipng_program(program: str, filepath: str) -> None:
    """Compress a spectral in place with the oxipng program; on any failure leave the file as it was."""
    try:
        with anyio.fail_after(OXIPNG_PROGRAM_TIMEOUT):
            result = await anyio.run_process(
                [program, "-o", "2", "--strip", "all", filepath], check=False, stdout=DEVNULL, stderr=DEVNULL
            )
        failed = result.returncode != 0
    except (TimeoutError, OSError):
        failed = True
    if failed:
        click.secho(
            f"Could not compress {os.path.basename(filepath)} with oxipng; it is uploaded as it is.", fg="yellow"
        )


async def _compress_single_spectral(filepath: str, _idx: int, program: str | None = None) -> None:
    """Compress a single spectral PNG image with pyoxipng in a thread, or with the oxipng program.

    Args:
        filepath: Path to the PNG file to compress.
        _idx: Unused index parameter for process_files compatibility.
        program: Path to the oxipng program, used when pyoxipng is not installed.
    """
    if oxipng is None:
        assert program is not None
        return await _compress_with_oxipng_program(program, filepath)
    func = partial(oxipng.optimize, filepath, level=2, strip=oxipng.StripChunks.all())
    return await anyio.to_thread.run_sync(func)


async def _compress_spectrals(spectrals_path: str, spectral_ids: dict[int, str] | None = None) -> None:
    """Compress spectral PNG images in a directory.

    Args:
        spectrals_path: Path to the directory containing spectral PNG files.
        spectral_ids: If provided, only compress the Full/Zoom images for
            these track IDs instead of every PNG in the folder. This is used
            to avoid compressing spectrals that were only generated for
            viewing and were never selected for upload.
    """
    if spectral_ids:
        files = [
            fname
            for sid in spectral_ids
            for fname in (f"{sid:02d} Full.png", f"{sid:02d} Zoom.png")
            if os.path.isfile(os.path.join(spectrals_path, fname))
        ]
    else:
        files = [f for f in os.listdir(spectrals_path) if f.endswith(".png")]
    if not files:
        return
    program = None
    if oxipng is None:
        program = shutil.which("oxipng")
        if program is None:
            _notify_spectrals_not_compressed()
            return

    filepaths = [os.path.join(spectrals_path, f) for f in files]

    await process_files(
        filepaths,
        partial(_compress_single_spectral, program=program) if program else _compress_single_spectral,
        "Compressing spectral images",
    )

    click.secho("Finished compressing spectrals.", fg="green")


def get_spectrals_path(path):
    """Get the path to the spectrals folder for an album."""
    base_name = os.path.basename(path.rstrip("/"))
    if cfg.directory.tmp_dir and os.path.isdir(cfg.directory.tmp_dir):
        # Create a unique subfolder for this album
        return os.path.join(cfg.directory.tmp_dir, f"spectrals_{base_name}")
    if cfg.directory.protects(path):
        # Never inside a library album: the folder is replaced, then deleted.
        return os.path.join(cfg.directory.download_directory, f"spectrals_{base_name}")
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
    spectrals.set_active_spectrals(
        all_spectral_ids,
        [sid for sid in all_spectral_ids if os.path.isfile(os.path.join(specs_path, spectrum_plot_name(sid)))],
    )

    runner = None
    try:
        try:
            runner = await create_app_async(specs_path)
        except OSError as e:
            port = cfg.upload.web_interface.port
            if e.errno == errno.EADDRINUSE:
                click.secho(
                    f"\nFailed to start web server: port {port} is already in use ({e}). "
                    "Please check if another process is using this port.",
                    fg="red",
                    bold=True,
                )
            elif e.errno == errno.EACCES:
                click.secho(
                    f"\nFailed to start web server: permission denied for port {port} ({e}). "
                    "Try using a non-privileged port (>1024).",
                    fg="red",
                    bold=True,
                )
            else:
                click.secho(
                    f"\nFailed to start web server on port {port}: {e!r}",
                    fg="red",
                    bold=True,
                )
            return
        url = f"http://{cfg.upload.web_interface.effective_host}:{cfg.upload.web_interface.port}/spectrals"
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
    finally:
        if runner is not None:
            await runner.cleanup()


async def upload_spectrals(
    spectrals_path: str,
    spectral_ids: dict[int, str] | None,
    tracker: str | None = None,
) -> dict[int, list[str]] | None:
    """Upload spectral images to image host.

    Args:
        spectrals_path: Path to spectrals folder.
        spectral_ids: Dict mapping spectral IDs to filenames.
        tracker: The site code of the tracker the spectrals are for, whose specs host they go to. None
            uploads them to the shared specs_uploader.

    Returns:
        Dict mapping spectral IDs to uploaded URLs, or None. A dry run uploads nothing: the URLs are what stands
        in for them.
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

    if dryrun.active():
        host = cfg.image.host_for(tracker, "specs_uploader")
        dryrun.say(f"not uploading the spectrals of {len(spectrals_list)} track(s) to {host}.")
        return {sid: [dryrun.image_url(path, host) for path in paths] for sid, _filename, paths in spectrals_list}
    try:
        return await upload_spectral_imgs(spectrals_list, tracker=tracker)
    except ImageUploadFailed as e:
        click.secho(f"Failed to upload spectral: {e}", fg="red")
        return None


def _default_spectral_selection(spectral_ids: dict[int, str], lossy_master: bool | None) -> str:
    """Get the default answer to the spectral IDs prompt: default_spectral_ids, else one based on lossy_master.

    Configured track IDs this release does not have are left out, and if none are left, the default is
    the one used when nothing is configured.
    """
    context_default = "*" if lossy_master else "+"
    configured = cfg.image.default_spectral_ids
    if configured is None:
        return context_default
    if configured in ("*", "+", "0"):
        return configured
    track_ids = [i for i in configured.split() if int(i) in spectral_ids]
    return " ".join(track_ids) if track_ids else context_default


async def prompt_spectrals(spectral_ids, lossy_master, check_lma, force_prompt_lossy_master=False, hosts=None):
    """Ask which spectral IDs the user wants to upload, to hosts (see check_spectrals)."""
    hosts = hosts or cfg.image.specs_uploader
    while True:
        ids = (
            "*"
            if cfg.upload.yes_all and not force_prompt_lossy_master
            else await click.prompt(
                click.style(
                    f"What spectral IDs would you like to upload to {hosts}? "
                    '(space-separated list of IDs, "0" for none, "*" for all, or "+" for a randomized selection)',
                    fg="magenta",
                ),
                default=_default_spectral_selection(spectral_ids, lossy_master),
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


async def prompt_lossy_master(force_prompt_lossy_master=False, offer_deletion=True, marks_found=False):
    """Ask whether the release is lossy mastered: True, False, or None to reopen the spectrals.

    offer_deletion: Whether to offer deleting the music folder, which an uploaded torrent may seed from.
    marks_found: Whether the frequency analysis found a lossy encoder's marks. It makes "yes" the default, and
        the question is then asked even with yes_all, which otherwise answers "no" without asking.
    """
    while True:
        flush_stdin()
        r = (
            "n"
            if cfg.upload.yes_all and not force_prompt_lossy_master and not marks_found
            else (
                await click.prompt(
                    click.style(
                        "\nIs this release lossy mastered? "
                        + ("[Y]es, [n]o" if marks_found else "[y]es, [N]o")
                        + ", [r]eopen spectrals, [a]bort"
                        + (", [d]elete music folder" if offer_deletion else ""),
                        fg="magenta",
                    ),
                    type=click.STRING,
                    default="y" if marks_found else "n",
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
        elif r == "d" and offer_deletion:
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
    """Report torrent for lossy WEB/master approval. A dry run prints the report instead.

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
    if dryrun.active():
        dryrun.say(f"not reporting the torrent to {gazelle_site.site_string} for lossy master approval. The report:")
        click.echo(textwrap.indent(comment, "  "))
        return
    try:
        await gazelle_site.report_lossy_master(torrent_id, comment, source)
    except UnknownOutcomeError as err:
        # The upload itself went through, so the rest of the flow (seeding above all) goes on.
        click.secho(
            f"\nCould not tell whether {gazelle_site.site_string} took the lossy master report ({err}): it may "
            f"have been filed. Check {gazelle_site.base_url}/torrents.php?torrentid={torrent_id} before reporting "
            "it again.",
            fg="red",
            bold=True,
        )
        return
    except RequestError as err:
        # Not a failed upload: the torrent is up, and the rest of the flow goes on.
        click.secho(
            f"\n{gazelle_site.site_string} did not take the lossy master report for "
            f"{gazelle_site.base_url}/torrents.php?torrentid={torrent_id} ({err}). Report it by hand with this text:",
            fg="red",
            bold=True,
        )
        click.echo(comment)
        return
    click.secho("\nReported upload for Lossy Master/WEB Approval Request.", fg="cyan")


async def generate_lossy_approval_comment(source_url, filenames, force_prompt_lossy_master=False):
    while True:
        comment = (
            ""
            # Without a source URL, an empty comment is refused: yes_all would refuse it forever.
            if cfg.upload.yes_all and not force_prompt_lossy_master and source_url
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
    uploads: SpectralUploads | None = None,
) -> tuple[bool, str | None, dict[int, list[str]] | None, dict[int, str] | None]:
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
        uploads: The spectral uploads of the upload run this check is part of, for its other trackers to reuse.
            Without one, the spectrals are only for this tracker.

    Returns:
        Tuple of (lossy_master, lossy_comment, spectral_urls, spectral_ids).
    """
    if uploads is None:
        uploads = SpectralUploads(path, [gazelle_site.site_code])
    # The uploaded torrent seeds from path: the check must not offer to delete it.
    lossy_master, spectral_ids = await check_spectrals(
        path,
        track_data,
        None,
        spectral_ids,
        force_prompt_lossy_master=True,
        format=format,
        hosts=uploads.hosts_text(),
        offer_deletion=False,
    )
    if not lossy_master and not spectral_ids:
        # Nothing to upload, for any tracker: the spectrals made for the check go, before a torrent could take them.
        await uploads.close()
        return False, None, None, None

    lossy_comment = None
    if lossy_master:
        lossy_comment = await generate_lossy_approval_comment(
            source_url, list(track_data.keys()), force_prompt_lossy_master=True
        )
        click.echo()

    spectral_urls = await uploads.urls_for(gazelle_site.site_code, spectral_ids)

    if spectral_urls:
        spectrals_bbcode = make_spectral_bbcode(spectral_ids, spectral_urls)
        permalink = f"{gazelle_site.base_url}/torrents.php?torrentid={torrent_id}"
        try:
            await gazelle_site.append_to_torrent_description(torrent_id, spectrals_bbcode)
        except UnknownOutcomeError as err:
            click.secho(
                f"\nCould not tell whether {gazelle_site.site_string} took the description edit for {permalink} "
                f"({err}): it may not have been updated; check the description before pasting this in "
                "by hand:",
                fg="red",
                bold=True,
            )
            click.echo(spectrals_bbcode)
        except RequestError as err:
            click.secho(
                f"\nThe description for {permalink} was not updated on {gazelle_site.site_string} ({err}). "
                "Paste this in by hand:",
                fg="red",
                bold=True,
            )
            click.echo(spectrals_bbcode)

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
    return bool(lossy_master), lossy_comment, spectral_urls, spectral_ids
