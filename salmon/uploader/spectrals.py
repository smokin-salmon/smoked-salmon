import asyncio
import os
import platform

# used by post upload stuff might move.
import random
import re
import shutil
import subprocess
import time
from os.path import dirname, join

import click
import oxipng

from salmon import cfg
from salmon.common import flush_stdin, get_audio_files, prompt_async
from salmon.common.figles import process_files
from salmon.errors import (
    AbortAndDeleteFolder,
    ImageUploadFailed,
    UploadError,
    WebServerIsAlreadyRunning,
)
from salmon.images import upload_spectrals as upload_spectral_imgs
from salmon.web import create_app_async, spectrals

THREADS = [None] * cfg.upload.simultaneous_threads


def check_spectrals(
    path,
    audio_info,
    lossy_master=None,
    spectral_ids=None,
    check_lma=True,
    force_prompt_lossy_master=False,
    format="FLAC",
):
    """
    Run the spectral checker functions. Generate the spectrals and ask whether or
    not the files are lossy. If the IDs were not all provided, prompt for spectrals
    to upload.
    """
    if format != "FLAC":
        click.secho(
            "Spectrals are only generated for FLAC files. Skipping...",
            fg="cyan",
        )
        return None, None
    click.secho("\nChecking lossy master / spectrals...", fg="cyan", bold=True)
    spectrals_path = create_specs_folder(path)
    if not spectral_ids:
        all_spectral_ids = generate_spectrals_all(path, spectrals_path, audio_info)
        while True:
            view_spectrals(spectrals_path, all_spectral_ids)
            if lossy_master is None and check_lma:
                lossy_master = prompt_lossy_master(force_prompt_lossy_master)
                if lossy_master is not None:
                    break
            else:
                break
    else:
        if lossy_master is None:
            lossy_master = prompt_lossy_master(force_prompt_lossy_master)

    if not spectral_ids:
        spectral_ids = prompt_spectrals(
            all_spectral_ids,
            lossy_master,
            check_lma,
            force_prompt_lossy_master=force_prompt_lossy_master,
        )
    else:
        spectral_ids = generate_spectrals_ids(path, spectral_ids, spectrals_path, audio_info)

    return lossy_master, spectral_ids


def handle_spectrals_upload_and_deletion(spectrals_path, spectral_ids, delete_spectrals=True):
    spectral_urls = upload_spectrals(spectrals_path, spectral_ids)
    if delete_spectrals and os.path.isdir(spectrals_path):
        shutil.rmtree(spectrals_path, ignore_errors=True)
        time.sleep(0.5)
        if os.path.isdir(spectrals_path):
            shutil.rmtree(spectrals_path)
            time.sleep(0.5)
    return spectral_urls


def generate_spectrals_all(path, spectrals_path, audio_info):
    """Wrapper function to generate all spectrals."""
    files_li = get_audio_files(path, True)
    return _generate_spectrals(path, files_li, spectrals_path, audio_info)


def generate_spectrals_ids(path, track_ids, spectrals_path, audio_info):
    """Wrapper function to generate a specific set of spectrals."""
    if track_ids == (0,):
        click.secho("Uploading no spectrals...", fg="yellow")
        return {}

    wanted_filenames = get_wanted_filenames(list(audio_info), track_ids)
    files_li = [fn for fn in get_audio_files(path) if fn in wanted_filenames]
    return _generate_spectrals(path, files_li, spectrals_path, audio_info)


def get_wanted_filenames(filenames, track_ids):
    """Get the filenames from the spectrals specified as cli options."""
    try:
        return {filenames[i - 1] for i in track_ids}
    except IndexError:
        raise UploadError("Spectral IDs out of range.") from None


def _generate_spectral_for_file(path, filename, spectrals_path, audio_info, idx):
    """
    Function to generate spectrals for a single file.
    """
    zoom_startpoint = calculate_zoom_startpoint(audio_info[filename])

    full_spectral_path = os.path.join(spectrals_path, f"{idx + 1:02d} Full.png")
    zoom_spectral_path = os.path.join(spectrals_path, f"{idx + 1:02d} Zoom.png")

    # Run the subprocess for generating the spectrals
    subprocess.run(
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
        check=True,  # Raise error if subprocess fails
    )

    return (idx + 1, filename)  # Return the filename to track progress


def _generate_spectrals(path, files_li, spectrals_path, audio_info):
    """
    Iterate over the filenames and generate the spectrals.
    """
    spectral_ids = {}

    results = process_files(
        files_li,
        lambda file, idx: _generate_spectral_for_file(path, file, spectrals_path, audio_info, idx),
        "Generating Spectrals",
    )

    click.secho("Finished generating spectrals.", fg="green")
    if cfg.upload.compression.compress_spectrals:
        _compress_spectrals(spectrals_path)

    for track_num, filename in results:
        spectral_ids[track_num] = filename

    sorted_spectrals = dict(sorted(spectral_ids.items()))

    return sorted_spectrals


def _compress_spectrals(spectrals_path):
    """
    Iterate over the spectrals directory and compress them using pyoxipng.
    """
    files = [f for f in os.listdir(spectrals_path) if f.endswith(".png")]
    if not files:
        return

    filepaths = [os.path.join(spectrals_path, f) for f in files]

    process_files(
        filepaths,
        lambda filepath, idx: oxipng.optimize(filepath, level=2, strip=oxipng.StripChunks.all()),
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


def view_spectrals(spectrals_path, all_spectral_ids):
    """Open the generated spectrals in an image viewer."""
    if not cfg.upload.native_spectrals_viewer:
        asyncio.run(_open_specs_in_web_server(spectrals_path, all_spectral_ids))
    elif platform.system() == "Darwin":
        _open_specs_in_preview(spectrals_path)
    elif platform.system() == "Windows":
        _open_specs_in_windows(spectrals_path)
    else:
        _open_specs_in_feh(spectrals_path)


def _open_specs_in_preview(spectrals_path):
    args = [
        "qlmanage",
        "-p",
        f"{spectrals_path}/*",
    ]
    with open(os.devnull, "w") as devnull:
        subprocess.Popen(args, stdout=devnull, stderr=devnull)


def _open_specs_in_feh(spectrals_path):
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
    with open(os.devnull, "w") as devnull:
        subprocess.Popen(args, stdout=devnull, stderr=devnull)


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

    shutdown = True
    try:
        try:
            os.symlink(specs_path, symlink_path)
        except FileExistsError:
            os.unlink(symlink_path)
            os.symlink(specs_path, symlink_path)
        try:
            runner = await create_app_async()
        except WebServerIsAlreadyRunning:
            shutdown = False
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
        if shutdown:
            await runner.cleanup()
    finally:
        os.unlink(symlink_path)


def upload_spectrals(spectrals_path, spectral_ids):
    """
    Create the tuples of spectral ids and filenames, then send them to the
    spectral uploader.
    """
    if not spectral_ids:
        return None

    spectrals = []
    for sid, filename in spectral_ids.items():
        spectrals.append(
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
        return upload_spectral_imgs(spectrals)
    except ImageUploadFailed as e:
        return click.secho(f"Failed to upload spectral: {e}", fg="red")


def prompt_spectrals(spectral_ids, lossy_master, check_lma, force_prompt_lossy_master=False):
    """Ask which spectral IDs the user wants to upload."""
    while True:
        ids = (
            "*"
            if cfg.upload.yes_all and not force_prompt_lossy_master
            else click.prompt(
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


def prompt_lossy_master(force_prompt_lossy_master=False):
    while True:
        flush_stdin()
        r = (
            "n"
            if cfg.upload.yes_all and not force_prompt_lossy_master
            else click.prompt(
                click.style(
                    "\nIs this release lossy mastered? [y]es, [N]o, [r]eopen spectrals, [a]bort, [d]elete music folder",
                    fg="magenta",
                ),
                type=click.STRING,
                default="n",
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


def report_lossy_master(
    gazelle_site,
    torrent_id,
    spectral_urls,
    spectral_ids,
    source,
    comment,
    source_url=None,
):
    """
    Generate the report description and call the function to report the torrent
    for lossy WEB/master approval.
    """

    comment = _add_spectral_links_to_lossy_comment(comment, source_url, spectral_urls, spectral_ids)
    asyncio.run(gazelle_site.report_lossy_master(torrent_id, comment, source))
    click.secho("\nReported upload for Lossy Master/WEB Approval Request.", fg="cyan")


def generate_lossy_approval_comment(source_url, filenames, force_prompt_lossy_master=False):
    comment = (
        ""
        if cfg.upload.yes_all and not force_prompt_lossy_master
        else click.prompt(
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
    if not (comment or source_url):
        click.secho(
            "This release was not uploaded with go, gos, or the queue, so you must add a comment about the source.",
            fg="red",
        )
        return generate_lossy_approval_comment(source_url, filenames)
    return comment


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


def post_upload_spectral_check(
    gazelle_site, path, torrent_id, spectral_ids, track_data, source, source_url, format="FLAC"
):
    """
    Offers generation and addition of spectrals after upload.
    As this is post upload, we have time to ask if this is a lossy master, so force prompt.
    """
    lossy_master, spectral_ids = check_spectrals(
        path, track_data, None, spectral_ids, force_prompt_lossy_master=True, format=format
    )
    if not lossy_master and not spectral_ids:
        return None, None, None, None

    lossy_comment = None
    if lossy_master:
        lossy_comment = generate_lossy_approval_comment(
            source_url, list(track_data.keys()), force_prompt_lossy_master=True
        )
        click.echo()

    spectrals_path = get_spectrals_path(path)
    spectral_urls = handle_spectrals_upload_and_deletion(spectrals_path, spectral_ids)

    if spectral_urls:
        spectrals_bbcode = make_spectral_bbcode(spectral_ids, spectral_urls)
        asyncio.run(gazelle_site.append_to_torrent_description(torrent_id, spectrals_bbcode))

    if lossy_master:
        report_lossy_master(
            gazelle_site,
            torrent_id,
            spectral_urls,
            spectral_ids,
            source,
            lossy_comment,
            source_url,
        )
    return lossy_master, lossy_comment, spectral_urls, spectral_ids
