import asyncio
import html
import os
import shutil
import subprocess
from urllib import parse

import click
import pyperclip

import salmon.checks
import salmon.converter
import salmon.database
import salmon.play
import salmon.search
import salmon.sources
import salmon.tagger
import salmon.uploader
import salmon.web  # noqa F401
from salmon import cfg
from salmon.common import commandgroup, str_to_int_if_int
from salmon.common import compress as recompress
from salmon.config import find_config_path, get_default_config_path, get_user_cfg_path
from salmon.database import DB_PATH
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.combine import combine_metadatas
from salmon.tagger.metadata import clean_metadata, remove_various_artists
from salmon.tagger.retagger import create_artist_str
from salmon.tagger.sources import run_metadata
from salmon.uploader.seedbox import UploaderGenerator
from salmon.uploader.spectrals import (
    check_spectrals,
    get_spectrals_path,
    handle_spectrals_upload_and_deletion,
    post_upload_spectral_check,
)
from salmon.uploader.upload import generate_source_links


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True), nargs=1)
@click.option("--no-delete-specs", "-nd", is_flag=True)
@click.option("--format-output", "-f", is_flag=True)
def specs(path, no_delete_specs, format_output):
    """Generate and open spectrals for a folder"""
    audio_info = gather_audio_info(path, True)
    _, sids = check_spectrals(path, audio_info, check_lma=False)
    spath = get_spectrals_path(path)
    spectral_urls = handle_spectrals_upload_and_deletion(spath, sids, delete_spectrals=not no_delete_specs)

    filenames = list(audio_info.keys())
    if spectral_urls:
        output = []
        for spec_id, urls in spectral_urls.items():
            if format_output:
                output.append(f"[hide={filenames[spec_id - 1]}][img={'][img='.join(urls)}][/hide]")
            else:
                output.append(f"{filenames[spec_id - 1]}: {' '.join(urls)}")
        output = "\n".join(output)
        click.secho(output)
        if cfg.upload.description.copy_uploaded_url_to_clipboard:
            pyperclip.copy(output)

    if no_delete_specs:
        click.secho(f"Spectrals saved to {spath}", fg="green")


@commandgroup.command()
@click.argument("urls", type=click.STRING, nargs=-1)
def descgen(urls):
    """Generate a description from metadata sources"""
    if not urls:
        return click.secho("You must specify at least one URL", fg="red")
    tasks = [run_metadata(url, return_source_name=True) for url in urls]
    metadatas = asyncio.run(asyncio.gather(*tasks))
    metadata = clean_metadata(combine_metadatas(*((s, m) for m, s in metadatas)))
    remove_various_artists(metadata["tracks"])

    description = "[b][size=4]Tracklist[/b]\n\n"
    multi_disc = len(metadata["tracks"]) > 1
    for dnum, disc in metadata["tracks"].items():
        for tnum, track in disc.items():
            if multi_disc:
                description += (
                    f"[b]{str_to_int_if_int(str(dnum), zpad=True)}-{str_to_int_if_int(str(tnum), zpad=True)}.[/b] "
                )
            else:
                description += f"[b]{str_to_int_if_int(str(tnum), zpad=True)}.[/b] "

            description += f"{create_artist_str(track['artists'])} - {track['title']}\n"
    if metadata["comment"]:
        description += f"\n{metadata['comment']}\n"
    if metadata["urls"]:
        description += "\n[b]More info:[/b] " + generate_source_links(metadata["urls"])
    click.secho("\nDescription:\n", fg="yellow", bold=True)
    click.echo(description)
    if cfg.upload.description.copy_uploaded_url_to_clipboard:
        pyperclip.copy(description)


@commandgroup.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True))
def compress(path):
    """Recompress a directory of FLACs to level 8"""
    for root, _, figles in os.walk(path):
        for f in sorted(figles):
            if os.path.splitext(f)[1].lower() == ".flac":
                filepath = os.path.join(root, f)
                click.secho(f"Recompressing {filepath[len(path) + 1 :]}...")
                recompress(filepath)


@commandgroup.command()
@click.option(
    "--torrent-id",
    "-i",
    default=None,
    help="Torrent id or URL, tracker from URL will overule -t flag.",
)
@click.option(
    "--tracker",
    "-t",
    help=f"Tracker choices: ({'/'.join(salmon.trackers.tracker_list)})",
)
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    nargs=1,
    default=".",
)
def checkspecs(tracker, torrent_id, path):
    """Will check and upload the spectrals of a given torrent\n
    Based on local files, not the ones on the tracker.
    By default checks the folder the script is run from.
    Can add spectrals to a torrent description and report a torrent as lossy web.
    """
    if not torrent_id:
        click.secho("No torrent id provided.", fg="red")
        torrent_id = click.prompt(
            click.style(
                """Input a torrent id or a URL containing one.
                Tracker in a URL will override -t flag.""",
                fg="magenta",
                bold=True,
            ),
        )
    if "/torrents.php" in torrent_id:
        base_url = parse.urlparse(torrent_id).netloc
        if base_url in salmon.trackers.tracker_url_code_map:
            # this will overide -t tracker
            tracker = salmon.trackers.tracker_url_code_map[base_url]
        else:
            click.echo("Unrecognised tracker!")
            raise click.Abort
        torrent_id = int(parse.parse_qs(parse.urlparse(torrent_id).query)["torrentid"][0])
    elif torrent_id.strip().isdigit():
        torrent_id = int(torrent_id)
    else:
        click.echo("Not a valid torrent!")
        raise click.Abort
    tracker = salmon.trackers.validate_tracker(None, "tracker", tracker)
    gazelle_site = salmon.trackers.get_class(tracker)()
    req = asyncio.run(gazelle_site.request("torrent", id=torrent_id))
    path = os.path.join(path, html.unescape(req["torrent"]["filePath"]))
    source_url = None
    source = req["torrent"]["media"]
    click.echo(f"Generating spectrals for {source} sourced: {path}")
    track_data = gather_audio_info(path)
    post_upload_spectral_check(gazelle_site, path, torrent_id, None, track_data, source, source_url)


def _backup_config(config_path):
    backup_index = 1
    while os.path.exists(f"{config_path}.bak.{backup_index}"):
        backup_index += 1
    shutil.move(config_path, f"{config_path}.bak.{backup_index}")
    click.secho(f"Existing config file renamed to config.py.bak.{backup_index}", fg="yellow")


@commandgroup.command()
@click.option(
    "--tracker",
    "-t",
    type=click.Choice(salmon.trackers.tracker_list, case_sensitive=False),
    help=f"Choices: ({'/'.join(salmon.trackers.tracker_list)})",
)
@click.option(
    "--metadata",
    "-m",
    is_flag=True,
    help="Test metadata sources connections (Discogs, Tidal, Qobuz).",
)
@click.option(
    "--seedbox",
    "-s",
    is_flag=True,
    help="Test seedbox connections.",
)
@click.option(
    "--reset",
    "-r",
    is_flag=True,
    help="Reset the config file to the default template. Will create a backup of the current config file.",
)
def checkconf(tracker, metadata, seedbox, reset):
    """Check the config and the connection to the trackers, metadata sources, and seedbox.\n
    Will output debug information if the connection fails.
    Use the -r flag to reset/create the whole config file.
    Use the -m flag to test metadata sources connections.
    Use the -s flag to test seedbox connections.
    """
    if reset:
        click.secho("Resetting new config.toml file", fg="cyan", bold=True)

        config_path = find_config_path()
        config_template = get_default_config_path()

        if os.path.exists(config_path):
            _backup_config(config_path)

        if not os.path.exists(config_template):
            click.secho("Error: config.default.toml template not found.", fg="red")
            return

        shutil.copy(config_template, config_path)
        click.secho(
            "A new config.toml file has been created from the template. Please update it with your custom settings.",
            fg="green",
        )
        return

    cfg.upload.debug_tracker_connection = True

    # Test trackers if no specific test type is requested or if tracker is specified
    if not (metadata or seedbox) or tracker:
        trackers = [tracker] if tracker else salmon.trackers.tracker_list

        for t in trackers:
            click.secho(f"\n[ Testing Tracker: {t} ]", fg="cyan", bold=True)

            try:
                salmon.trackers.get_class(t)()
                click.secho(f"\n✔ Successfully checked {t}", fg="green", bold=True)
            except Exception as e:
                click.secho(f"\n✖ Error testing {t}: {e}", fg="red", bold=True)

            click.secho("-" * 50, fg="yellow")  # Separator for readability

    # Test metadata sources
    if metadata or not (tracker or seedbox):
        _test_metadata_sources()

    # Test seedbox connections
    if seedbox or not (tracker or metadata):
        _test_seedbox_connections()


def _iter_which(deps):
    for dep in deps:
        present = shutil.which(dep)
        if present:
            click.secho(f"{dep} ✓", fg="green")
        else:
            click.secho(f"{dep} ✘", fg="red")


def _test_metadata_sources():
    """Test metadata sources connections (Discogs, Tidal, Qobuz)"""
    click.secho("\n[ Testing Metadata Sources ]", fg="cyan", bold=True)

    metadata_sources = {
        "Discogs": {
            "class": salmon.sources.DiscogsBase,
            "test_url": "https://www.discogs.com/release/432932",
            "config_check": lambda: bool(cfg.metadata.discogs_token),
        },
        "Tidal": {
            "class": salmon.sources.TidalBase,
            "test_url": "http://www.tidal.com/album/75194842",
            "config_check": lambda: bool(cfg.metadata.tidal.token and cfg.metadata.tidal.fetch_regions),
        },
        "Qobuz": {
            "class": salmon.sources.QobuzBase,
            "test_url": "https://www.qobuz.com/album/-/0886446576442",
            "config_check": lambda: bool(cfg.metadata.qobuz.app_id and cfg.metadata.qobuz.user_auth_token),
        },
    }

    for source_name, source_info in metadata_sources.items():
        click.secho(f"\n  Testing {source_name}...", fg="yellow")

        try:
            # Check if required config is present
            if not source_info["config_check"]():
                click.secho(f"  ✖ {source_name}: Missing required configuration", fg="red", bold=True)
                continue

            # Try to initialize the source class
            source_instance = source_info["class"]()

            # For a basic connection test, try to create soup with a test URL
            try:
                asyncio.run(source_instance.create_soup(source_info["test_url"]))
                click.secho(f"  ✔ {source_name}: Connection successful", fg="green", bold=True)
            except Exception as inner_e:
                click.secho(f"  ✖ {source_name}: Error - {inner_e}", fg="red", bold=True)

        except Exception as e:
            click.secho(f"  ✖ {source_name}: Failed to initialize - {e}", fg="red", bold=True)

    click.secho("-" * 50, fg="yellow")


def _test_seedbox_connections():
    """Test seedbox connections"""
    click.secho("\n[ Testing Seedbox Connections ]", fg="cyan", bold=True)

    if not cfg.seedbox:
        click.secho("  No seedboxes configured", fg="yellow")
        click.secho("-" * 50, fg="yellow")
        return

    for i, seedbox_config in enumerate(cfg.seedbox):
        if not seedbox_config.enabled:
            click.secho(f"\n  Seedbox {i + 1} ({seedbox_config.name}): Disabled", fg="yellow")
            continue

        click.secho(f"\n  Testing Seedbox {i + 1} ({seedbox_config.name})...", fg="yellow")
        click.secho(f"    Type: {seedbox_config.type}", fg="cyan")
        click.secho(f"    URL: {seedbox_config.url}", fg="cyan")

        try:
            # Test the uploader initialization
            UploaderGenerator.get_uploader(
                seedbox_config.type, seedbox_config.url, seedbox_config.extra_args, seedbox_config.torrent_client
            )

            if seedbox_config.type == "rclone":
                if shutil.which("rclone"):
                    click.secho("    ✔ Rclone executable found", fg="green")
                    # Test rclone config
                    try:
                        result = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=10)
                        if seedbox_config.url + ":" in result.stdout:
                            click.secho(f"    ✔ Rclone remote '{seedbox_config.url}' found", fg="green", bold=True)
                        else:
                            click.secho(f"    ✖ Rclone remote '{seedbox_config.url}' not found", fg="red", bold=True)
                    except Exception as rclone_e:
                        click.secho(f"    ✖ Rclone test failed: {rclone_e}", fg="red", bold=True)
                else:
                    click.secho("    ✖ Rclone executable not found", fg="red", bold=True)

        except Exception as e:
            click.secho(f"    ✖ Seedbox test failed: {e}", fg="red", bold=True)

    click.secho("-" * 50, fg="yellow")


@commandgroup.command()
def health():
    """Check the status of smoked-salmon's config files and command line dependencies"""

    try:
        config_path = find_config_path()
        click.echo(f"Config path: {config_path}")
    except FileNotFoundError:
        click.secho(f"Could not find config at {get_user_cfg_path()}", fg="red")

    if os.path.exists(DB_PATH):
        click.echo(f"DB path: {DB_PATH}")
    else:
        click.secho(f"Could not find database at {DB_PATH}", fg="red")

    click.echo()

    req_deps = ["curl", "ffmpeg", "flac", "git", "lame", "mp3val", "sox", "unzip"]
    opt_deps = ["puddletag", "feh", "rclone"]
    click.secho("Required Dependencies:", fg="cyan")
    _iter_which(req_deps)

    click.secho("\nOptional Dependencies:", fg="cyan")
    _iter_which(opt_deps)
