"""`salmon cross-upload`: upload a torrent that is on RED to OPS, or the reverse, from the files already on disk.

The run reads every release's torrent from SOURCE and checks it locally first (the data the form gets, the
files on disk, the log, the images), shows the plan, and only then sends anything to TARGET: per release, the
dupe check as `up` makes it, the upload of the source format, and the conversions asked for. The first failure
there stops the run, and says what is already up.

Ported from chodeus's fork (cross_upload.py), the version used on the live trackers: the form, the order of
the steps around the upload and the description are the fork's, apart from the differences
tests/test_cross_upload_fork_parity.py lists.
"""

import html
import re
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import anyio
import asyncclick as click
from bs4 import BeautifulSoup
from torf import TorfError, Torrent

import salmon.trackers
from salmon import cfg, dryrun
from salmon.common import commandgroup
from salmon.config.image_hosts import HOST_RULES
from salmon.constants import ARTIST_IMPORTANCES, ENCODINGS, FORMATS, SOURCES
from salmon.errors import DryRunRefused, ImageUploadFailed, RequestError, RequestFailedError, UploadError
from salmon.images import HOSTS, image_host_for_tracker
from salmon.images import red as red_images
from salmon.release_notification import get_version
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.tags import gather_tags
from salmon.uploader import (
    _check_logs,
    downconversion_format,
    execute_downconversion_tasks,
    finish_upload,
    get_downconversion_options,
    red_api_for_covers,
)
from salmon.uploader.dupe_checker import (
    _confirm_group_id,
    check_existing_group,
    generate_dupe_check_searchstrs,
    held_formats,
)
from salmon.uploader.seedbox import UploadManager
from salmon.uploader.spectrals import report_lossy_master
from salmon.uploader.staging import run_directory
from salmon.uploader.upload import (
    compile_files,
    concat_track_data,
    generate_description,
    generate_torrent,
    has_upload_footer,
    upload_footer,
)

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

# The trackers a release can go between. DIC is not one: its rules and API answers are not checked.
TRACKERS = ("RED", "OPS")
MAX_RELEASES = 5
# The most images one release may need fetched from SOURCE and uploaded again.
MAX_REHOSTED_IMAGES = 10

# The Gazelle musicInfo key of each artist role, in the order the form gets them.
ARTIST_FIELDS = {
    "artists": "main",
    "with": "guest",
    "remixedBy": "remixer",
    "composers": "composer",
    "conductor": "conductor",
    "dj": "djcompiler",
    "producer": "producer",
    "arranger": "arranger",
}

# The hosts each tracker serves its site, its images and its announces from (and their subdomains).
TRACKER_HOSTS = {"RED": ("redacted.sh", "flacsfor.me"), "OPS": ("orpheus.network", "opsfet.ch")}
# The HOST_RULES row of a tracker's own image host, which says where its images show. A tracker with none shows
# only its own images.
TRACKER_IMAGE_HOSTS = {"RED": "red"}
# The media a tracker names otherwise than SOURCES does: OPS's BD is RED's Blu-Ray.
TRACKER_MEDIA = {"OPS": {"Blu-Ray": "BD"}, "RED": {"BD": "Blu-Ray"}}
UPSTREAM_URL = "https://github.com/smokin-salmon/smoked-salmon"

# The one known broken shape an album description salmon wrote used to have (#597).
_BROKEN_TRACKLIST_HEADER = "[b][size=4]Tracklist[/b]"
_TRACKLIST_HEADER = "[b][size=4]Tracklist[/size][/b]"

_FILE_ENTRY = re.compile(r"(.+)\{\{\{(\d+)\}\}\}$", re.DOTALL)
_IMAGE_TAG = re.compile(r"\[img\]\s*(https?://[^\[\]\s]+?)\s*\[/img\]|\[img=(https?://[^\]\s]+)\]", re.IGNORECASE)
# What a description can link with, in the order it is read: an image (never a link), a link with its text, a
# link that is its own text, a link left open, one of Gazelle's tags that open the site's own pages by id (a torrent
# group, a torrent, a collage, a forum, a thread) or by name (a user, a rule of that site), a bare URL. An artist
# tag is left: it finds the artist by name, the same on both trackers.
_LINK_TOKEN = re.compile(
    r"(?P<image>\[img\][^\[\]]*\[/img\]|\[img=[^\]]*\])"
    r"|\[url=(?P<target>[^\]]*)\](?P<text>(?:(?!\[/?url[=\]]).)*?)\[/url\]"
    r"|\[url\](?P<own_target>[^\[\]]*)\[/url\]"
    r"|\[url=(?P<open_target>[^\]]*)\]"
    r"|\[(?P<by_id>torrent|pl|collage|forum|thread)(?:=[^\]]*)?\][^\[\]]*\[/(?P=by_id)\]"
    r"|\[(?P<by_name>user|rule)\](?P<name>[^\[\]]*)\[/(?P=by_name)\]"
    r"|(?P<url>https?://[^\s\[\]<>\"']+)",
    re.IGNORECASE | re.DOTALL,
)
_LOSSY_CLASSES = ("tl_lossymaster_approved", "tl_lossyweb_approved")
# The lossy report comment offered when the torrent description shows spectrals, which a cross-upload does not make.
SPECTRALS_COMMENT = "Spectrals are in the torrent description."
_LOSSY_TITLES = ("lossy master approved", "lossy web approved")
_IMAGE_MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"GIF87a": ".gif", b"GIF89a": ".gif"}


class CrossUploadRefused(Exception):
    """A release that cannot be cross-uploaded; the message says why."""


@dataclass
class Release:
    """A release that passed every check before anything is sent to TARGET."""

    label: str  # How the user named it: the id, URL or .torrent file
    response: dict[str, Any]  # SOURCE's torrent answer
    path: Path  # The album folder, resolved
    data: dict[str, Any]  # The upload form of the source format, before its images are rehosted
    rehost: list[tuple[str, str]] = field(default_factory=list)  # (field, URL) of each SOURCE image to rehost
    tasks: list[dict[str, Any]] = field(default_factory=list)  # The conversions asked for, as `up` makes them
    track_data: dict[str, Any] = field(default_factory=dict)  # Read from the files, when needed
    notes: list[str] = field(default_factory=list)  # What the plan says about it
    lossy_report: str | None = None  # The lossy report TARGET gets after the upload, if any

    @property
    def torrent(self) -> dict[str, Any]:
        return self.response["torrent"]

    @property
    def group(self) -> dict[str, Any]:
        return self.response["group"]


@commandgroup.command("cross-upload")
@click.argument("inputs", nargs=-1, required=True, metavar="INPUT...")
@click.argument("source", metavar="SOURCE_TRACKER", type=click.Choice(TRACKERS, case_sensitive=False))
@click.argument("target", metavar="TARGET_TRACKER", type=click.Choice(TRACKERS, case_sensitive=False))
@click.option(
    "--path",
    "-p",
    type=click.Path(exists=True, file_okay=False),
    help="The album folder, if it is not download_directory/<the torrent's folder>. One INPUT only.",
)
@click.option("--group-id", "-g", type=click.IntRange(min=1), help="The TARGET group to upload into. One INPUT only.")
@click.option(
    "--transcode",
    "transcodes",
    type=click.Choice(("320", "V0"), case_sensitive=False),
    multiple=True,
    help="Also upload this MP3 transcode of a FLAC; may be given twice.",
)
@click.option("--downconvert", is_flag=True, help="Also upload the lossless downconversions of a 24-bit FLAC.")
@click.option("--all", "all_formats", is_flag=True, help="Also upload every conversion `up` would offer.")
@click.option("-yyy", is_flag=True, help="Automatically pick the default answer for prompts.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Read from both trackers and build every upload, but send nothing: print what each upload would send.",
)
async def cross_upload(
    inputs: tuple[str, ...],
    source: str,
    target: str,
    path: str | None,
    group_id: int | None,
    transcodes: tuple[str, ...],
    downconvert: bool,
    all_formats: bool,
    yyy: bool,
    dry_run: bool,
) -> None:
    """Upload torrents that are on SOURCE_TRACKER to TARGET_TRACKER, from the files on disk.

    INPUT is a SOURCE torrent ID, a SOURCE torrent URL, or a SOURCE .torrent file; at most 5. The files must be
    the torrent's, unchanged, in download_directory (or at --path).

    \b
    Examples:
      salmon cross-upload 456 RED OPS
      salmon cross-upload 456 789 OPS RED --dry-run
    """
    source, target = source.upper(), target.upper()
    if source == target:
        raise click.UsageError("SOURCE_TRACKER and TARGET_TRACKER must be different trackers.")
    missing = [code for code in (source, target) if code not in salmon.trackers.tracker_list]
    if missing:
        raise click.UsageError(f"Not configured: {', '.join(missing)}. Add it under [tracker] in your config.")
    if len(inputs) > MAX_RELEASES:
        raise click.UsageError(f"At most {MAX_RELEASES} releases per run, not {len(inputs)}.")
    if (path or group_id) and len(inputs) > 1:
        raise click.UsageError("--path and --group-id go with a single INPUT.")
    if yyy:
        cfg.upload.yes_all = True

    source_site = salmon.trackers.get_class(source)()
    target_site = salmon.trackers.get_class(target)()
    # The same ID or file given twice is read once.
    items = list(dict.fromkeys(_input_item(value, source_site) for value in inputs))
    with dryrun.mode(dry_run):
        if dry_run:
            dryrun.say("reading from both trackers and sending nothing. Each upload's form is printed instead.")
        try:
            await _run(
                items,
                source_site,
                target_site,
                path=path,
                group_id=group_id,
                transcodes=tuple(t.upper() for t in transcodes),
                downconvert=downconvert,
                all_formats=all_formats,
            )
        except* DryRunRefused as refused:
            click.secho(f"\n{refused.exceptions[0]}", fg="red", bold=True)
            raise click.exceptions.Exit(1) from refused
        if dry_run:
            dryrun.say("done. Nothing was sent.")


def is_torrent_reference(value: str) -> bool:
    """A torrent ID or an http(s) URL, as opposed to a local path: decided without touching the filesystem."""
    value = value.strip()
    return value.isdigit() or urlparse(value).scheme in ("http", "https")


def _input_item(value: str, source_site: "BaseGazelleApi") -> int | Path:
    """The torrent ID an INPUT names, or the .torrent file it is.

    Raises:
        click.UsageError: If it is none of a SOURCE torrent ID, URL or .torrent file.
    """
    # A reference is never looked up on disk: "42" is torrent 42 even if a folder "42" exists.
    if is_torrent_reference(value):
        return _torrent_id(value, source_site)
    path = Path(value).expanduser()
    if path.is_file() and path.suffix.lower() == ".torrent":
        return path
    raise click.UsageError(f"{value} is not a {source_site.site_string} torrent ID or URL, or a .torrent file.")


def _torrent_id(value: str, source_site: "BaseGazelleApi") -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    parsed = urlparse(value)
    if parsed.hostname != urlparse(source_site.base_url).hostname:
        raise click.UsageError(f"Expected a torrent URL from {source_site.base_url}, an ID, or a .torrent file.")
    torrent_ids = parse_qs(parsed.query).get("torrentid")
    if not torrent_ids or not torrent_ids[0].isdigit():
        raise click.UsageError("The torrent URL must hold a numeric torrentid.")
    return int(torrent_ids[0])


async def _run(
    items: list[int | Path],
    source: "BaseGazelleApi",
    target: "BaseGazelleApi",
    *,
    path: str | None,
    group_id: int | None,
    transcodes: tuple[str, ...],
    downconvert: bool,
    all_formats: bool,
) -> None:
    """Check every release against SOURCE and the disk, confirm the plan, then upload each to TARGET."""
    releases: list[Release] = []
    seen: set[int] = set()
    for item in items:
        label = str(item)
        click.secho(f"\nReading {source.site_string} torrent {label}...", fg="cyan", bold=True)
        try:
            releases.append(
                await _prepare(
                    item,
                    source,
                    target,
                    seen,
                    path=path,
                    transcodes=transcodes,
                    downconvert=downconvert,
                    all_formats=all_formats,
                )
            )
        except (CrossUploadRefused, RequestFailedError, click.Abort) as error:
            # Only this release: SOURCE answered, about it.
            reason = str(error) or "stopped"
            click.secho(f"Not cross-uploading {label}: {reason}", fg="red", bold=True)
        except RequestError as error:
            # SOURCE itself (a rate limit, an outage, the login, TLS): every later release would fail the same way,
            # after sending its own requests. Nothing has gone to TARGET yet.
            click.secho(f"\nStopping: {source.site_string} could not be read ({error}).", fg="red", bold=True)
            raise click.exceptions.Exit(1) from error

    if not releases:
        click.secho("\nNothing to cross-upload.", fg="red")
        raise click.exceptions.Exit(1)
    _print_plan(releases, source, target, group_id)
    if not cfg.upload.yes_all and not click.confirm(
        click.style(
            f"\nCross-upload {'this' if len(releases) == 1 else 'these'} to {target.site_string}?", fg="magenta"
        ),
        default=True,
    ):
        raise click.Abort

    # Torrents and conversions go into a directory of the run's own in a dry run, removed when it ends.
    scratch = run_directory(str(releases[0].path)) if dryrun.active() else nullcontext(None)
    with scratch as scratch_dir, dryrun.writing_into(scratch_dir):
        seedbox = UploadManager()
        uploaded: list[str] = []
        try:
            for release in releases:
                await _upload(release, source, target, seedbox, uploaded, group_id=group_id)
        except (CrossUploadRefused, RequestError, UploadError, click.Abort) as error:
            click.secho(f"\nStopping: {str(error) or 'aborted'}. Nothing more is uploaded.", fg="red", bold=True)
            if uploaded and not dryrun.active():
                click.secho("Already uploaded:", fg="red")
                for url in uploaded:
                    click.echo(f"  {url}")
            raise click.exceptions.Exit(1) from error
        finally:
            await seedbox.execute_upload()


# Phase B: SOURCE reads and local checks, nothing sent to TARGET.


async def _prepare(
    item: int | Path,
    source: "BaseGazelleApi",
    target: "BaseGazelleApi",
    seen: set[int],
    *,
    path: str | None,
    transcodes: tuple[str, ...],
    downconvert: bool,
    all_formats: bool,
) -> Release:
    """Read a release from SOURCE and check everything that needs no TARGET request.

    Args:
        seen: The IDs of the torrents already in the run: one given again (as an ID and as its .torrent file)
            is dropped. This one's is added.

    Raises:
        CrossUploadRefused: If the release cannot go, saying why.
        RequestError: If SOURCE could not be read.
        click.Abort: If a check stopped and the user chose to stop.
    """
    response = await _source_response(item, source)
    torrent_id = int(response["torrent"]["id"])
    if torrent_id in seen:
        raise CrossUploadRefused(f"torrent {torrent_id} is already in this run")
    seen.add(torrent_id)
    release = Release(label=str(item), response=response, path=Path(), data={})
    release.notes = _check_torrent(response, source, target)
    release.data = compile_data(response, source, target)
    release.path = _release_path(response, path)
    _verify_release_files(response, release.path, target)

    if release.torrent["media"] == "CD":
        try:
            await _check_logs(str(release.path))
        except click.Abort:
            raise CrossUploadRefused("the log check stopped it") from None

    release.tasks = _conversion_tasks(release, transcodes, downconvert, all_formats)
    if not release.data["album_desc"]:
        release.data["album_desc"] = generate_description(_track_data(release), {"comment": None, "urls": []})
        release.notes.append(f"{source.site_string} has no album description: sending the tracklist from the tags")
    release.rehost = _images_to_rehost(release.data, source, target)
    await _check_lossy_approval(release, source, target)
    return release


async def _source_response(item: int | Path, source: "BaseGazelleApi") -> dict[str, Any]:
    if isinstance(item, int):
        return await source.api_call("torrent", params={"id": item})
    try:
        torrent = Torrent.read(item)
    except TorfError as error:
        raise CrossUploadRefused(f"{item} is not a readable .torrent file ({error})") from None
    source_host = urlparse(source.tracker_url).hostname
    announce_hosts = {urlparse(url).hostname for tier in torrent.trackers for url in tier}
    # qBittorrent saves a .torrent without its announce URL (it keeps trackers elsewhere): its source flag, which
    # the tracker (and salmon's own torrents) set, then says whose it is.
    if not announce_hosts and torrent.source != source.site_string:
        raise CrossUploadRefused(f"{item} has no announce URL and no {source.site_string} source flag")
    if announce_hosts and source_host not in announce_hosts:
        raise CrossUploadRefused(f"{item} does not announce to {source.site_string}")
    return await source.api_call("torrent", params={"hash": torrent.infohash.upper()})


def _check_torrent(response: dict[str, Any], source: "BaseGazelleApi", target: "BaseGazelleApi") -> list[str]:
    """Stop on what TARGET has no value for, and say what the plan should show about the rest.

    RED and OPS accept a CD with no log or a log under 100, and a torrent the other one flags as trumpable or
    reported: those go up as they are, and the plan says so.

    Returns:
        The plan's notes about the torrent.

    Raises:
        CrossUploadRefused: Naming the value.
    """
    group, torrent = response["group"], response["torrent"]
    media, format_, encoding = torrent.get("media"), torrent.get("format"), torrent.get("encoding")
    target_media = _target_media(media, target)
    if target_media not in SOURCES.values() and target_media not in TRACKER_MEDIA[target.site_code].values():
        raise CrossUploadRefused(f"salmon does not know {target.site_string} has the media {media!r}")
    if format_ not in FORMATS.values() or encoding not in ENCODINGS:
        raise CrossUploadRefused(f"salmon does not cross-upload {format_} {encoding}")

    on_source = f"on {source.site_string}"
    notes = []
    if target_media != media:
        notes.append(f"media {media} {on_source} is {target_media} on {target.site_string}")
    if media == "CD":
        if not torrent.get("hasLog"):
            notes.append(f"a CD with no rip log {on_source}")
        else:
            # RED's answer has no checksum key: only OPS's says whether the checksum is good.
            checksum = ""
            if "logChecksum" in torrent:
                checksum = ", checksum good" if torrent["logChecksum"] else ", checksum missing or bad"
            notes.append(f"log score {torrent.get('logScore')} {on_source}{checksum}")
    if torrent.get("trumpable"):
        reasons = ", ".join(str(reason) for reason in torrent.get("trumpable_reasons") or [])
        notes.append(f"trumpable {on_source}{f': {reasons}' if reasons else ''}")
    if torrent.get("reported"):
        notes.append(f"reported {on_source}")
    if group.get("vanityHouse"):
        notes.append(f"Vanity House {on_source}: sent to {target.site_string} without the flag")
    return notes


def _target_media(media: Any, target: "BaseGazelleApi") -> Any:
    """The media as TARGET names it."""
    return TRACKER_MEDIA[target.site_code].get(media, media)


def compile_data(response: dict[str, Any], source: "BaseGazelleApi", target: "BaseGazelleApi") -> dict[str, Any]:
    """Give TARGET's upload form for SOURCE's torrent: the fork's form, field for field, but for what it stops on.

    The torrent description is the fork's: a header naming SOURCE, the uploader and the source torrent, then
    SOURCE's description and the footer. Links to either tracker's site are taken out of the copied descriptions.

    Raises:
        CrossUploadRefused: If a value has no counterpart on TARGET.
    """
    group, torrent = response["group"], response["torrent"]
    artists = _artists(group, target)
    source_types = {value: name for name, value in source.release_types.items()}
    release_type = source_types.get(group.get("releaseType"))
    if release_type not in target.release_types:
        raise CrossUploadRefused(
            f"{target.site_string} has no release type {release_type or group.get('releaseType')!r}"
        )
    if not group.get("year"):
        raise CrossUploadRefused(f"the group has no year on {source.site_string}")
    # An original release (RED says so with remastered: false) takes the group's year. Without a year
    # otherwise, the edition is unknown, which the form cannot say.
    if not torrent.get("remasterYear") and torrent.get("remastered") is not False:
        raise CrossUploadRefused(f"its edition is unknown on {source.site_string}")

    sites = (source, target)
    description = without_tracker_links(html.unescape(torrent.get("description") or ""), sites)
    # The source description is the other tracker's, so it may already end with a footer.
    footer = "" if has_upload_footer(description) else f"\n\n{upload_footer()}"
    # RED's album description (bbBody) is HTML-escaped, as its torrent description is. OPS's (wikiBBcode) is the text
    # as written: unescaping it would turn a link's "&region=" into "®ion=".
    album_desc = html.unescape(group["bbBody"]) if group.get("bbBody") else group.get("wikiBBcode") or ""
    album_desc = without_tracker_links(album_desc, sites)

    return {
        "submit": True,
        "type": 0,
        "title": html.unescape(group["name"]),
        "artists[]": [name for name, _ in artists],
        "importance[]": [ARTIST_IMPORTANCES[role] for _, role in artists],
        "year": group["year"],
        "record_label": html.unescape(group.get("recordLabel") or ""),
        "catalogue_number": html.unescape(group.get("catalogueNumber") or ""),
        "releasetype": target.release_types[release_type],
        "remaster": True,
        "remaster_year": torrent.get("remasterYear") or group["year"],
        "remaster_title": html.unescape(torrent.get("remasterTitle") or ""),
        "remaster_record_label": html.unescape(torrent.get("remasterRecordLabel") or group.get("recordLabel") or ""),
        "remaster_catalogue_number": html.unescape(
            torrent.get("remasterCatalogueNumber") or group.get("catalogueNumber") or ""
        ),
        "format": torrent["format"],
        "bitrate": torrent["encoding"],
        "other_bitrate": None,
        "vbr": "VBR" in torrent["encoding"],
        "media": _target_media(torrent["media"], target),
        "tags": ",".join(group.get("tags") or []),
        "image": html.unescape(group.get("wikiImage") or ""),
        "album_desc": album_desc.replace(_BROKEN_TRACKLIST_HEADER, _TRACKLIST_HEADER),
        "release_desc": f"{_source_header(torrent, source, target)}\n\n{description}{footer}",
        **({"scene": True} if torrent.get("scene") else {}),
    }


def _source_header(torrent: dict[str, Any], source: "BaseGazelleApi", target: "BaseGazelleApi") -> str:
    """The fork's header: SOURCE to TARGET, who uploaded it there (linked to their profile), and its torrent."""
    name = html.unescape(torrent.get("username") or "the original uploader")
    uploader = f"[url={source.base_url}/user.php?id={torrent['userId']}]{name}[/url]" if torrent.get("userId") else name
    source_url = f"{source.base_url}/torrents.php?torrentid={torrent['id']}"
    return (
        f"[align=center][size=3][b]{source.site_code} → {target.site_code}[/b][/size]\n"
        f"[size=1]Original upload by {uploader} · [url={source_url}]View source torrent[/url]\n"
        f"Cross-uploaded with [url={UPSTREAM_URL}]smoked-salmon[/url] v{get_version()}[/size][/align]"
    )


def without_tracker_links(text: str, sites: tuple["BaseGazelleApi", ...]) -> str:
    """The description without its links to either tracker's site, which name SOURCE's pages and ids.

    A link keeps its text; a bare URL, or a link that is its own text, goes. A link with no scheme is one to the
    tracker's own pages (Gazelle's relative links): on TARGET it would point at TARGET's pages with SOURCE's ids,
    so it goes too, and so do Gazelle's tags that open the site's own pages by id. A user or rule tag keeps its
    text. Images are not links: they are left to the image rules.
    """

    def to_tracker(url: str) -> bool:
        url = url.strip().strip("\"'")
        return not urlparse(url).scheme or _tracker_of(url, sites) is not None

    def replace(match: re.Match[str]) -> str:
        if match["image"] is not None:
            return match[0]
        if match["target"] is not None:
            return without_tracker_links(match["text"], sites) if to_tracker(match["target"]) else match[0]
        if match["own_target"] is not None:
            return "" if to_tracker(match["own_target"]) else match[0]
        if match["open_target"] is not None:
            return "" if to_tracker(match["open_target"]) else match[0]
        if match["by_id"] is not None:
            return ""
        if match["by_name"] is not None:
            return match["name"]
        return "" if _tracker_of(match["url"], sites) is not None else match[0]

    return _LINK_TOKEN.sub(replace, text)


def _artists(group: dict[str, Any], target: "BaseGazelleApi") -> list[tuple[str, str]]:
    """The group's artists with their roles, in the form's order.

    Raises:
        CrossUploadRefused: On a role salmon does not know or TARGET does not have, or no artist at all.
    """
    music_info = group.get("musicInfo") or {}
    unknown = sorted(key for key, value in music_info.items() if value and key not in ARTIST_FIELDS)
    if unknown:
        raise CrossUploadRefused(f"salmon does not know the artist role {', '.join(unknown)}")
    artists = [
        (html.unescape(artist["name"]), role)
        for key, role in ARTIST_FIELDS.items()
        for artist in music_info.get(key) or []
    ]
    if not artists:
        raise CrossUploadRefused("the group has no artist")
    dropped = sorted({role for _, role in artists if role in target.unsupported_artist_roles})
    if dropped:
        raise CrossUploadRefused(f"{target.site_string} has no {', '.join(dropped)} role")
    return artists


def _release_path(response: dict[str, Any], path: str | None) -> Path:
    """Find the album folder: --path, or download_directory/<the torrent's folder>, symlinks resolved.

    Raises:
        CrossUploadRefused: If the folder is not there, is named otherwise, or resolves out of where it must be.
    """
    name = html.unescape(response["torrent"].get("filePath") or "")
    if not name:
        raise CrossUploadRefused("it is a torrent of a single file, with no folder: not supported")
    if name in (".", "..") or "/" in name or "\\" in name:
        raise CrossUploadRefused(f"its folder name {name!r} is not a folder name")
    if path is not None:
        folder = Path(path).expanduser().resolve()
    else:
        root = Path(cfg.directory.download_directory).expanduser().resolve()
        folder = (root / name).resolve()
        if folder.parent != root:
            raise CrossUploadRefused(f"{root / name} resolves to {folder}, outside download_directory")
    if folder.name != name:
        raise CrossUploadRefused(f"the folder is {folder}, but the torrent's folder is named {name!r}")
    if not folder.is_dir():
        raise CrossUploadRefused(f"the files are not at {folder}: give the album folder with --path")
    return folder


def _file_list(torrent: dict[str, Any]) -> dict[str, int]:
    """The torrent's files from its fileList, by path in the torrent's folder (NFC), with their size."""
    files: dict[str, int] = {}
    for entry in (torrent.get("fileList") or "").split("|||"):
        match = _FILE_ENTRY.match(entry.strip())
        if match:
            files[unicodedata.normalize("NFC", html.unescape(match[1]))] = int(match[2])
    return files


def _verify_release_files(response: dict[str, Any], folder: Path, target: "BaseGazelleApi") -> None:
    """Check the folder holds exactly the torrent's files: the same paths with the same sizes, and nothing more.

    The new torrent is made from the folder, so a retagged, renamed or added file would change it.

    Raises:
        CrossUploadRefused: Naming the first differences.
    """
    expected = _file_list(response["torrent"])
    if not expected:
        raise CrossUploadRefused("its file list could not be read")
    actual: dict[str, int] = {}
    for entry in sorted(folder.rglob("*")):
        relative = entry.relative_to(folder).as_posix()
        if not entry.resolve().is_relative_to(folder):
            raise CrossUploadRefused(f"{relative} is a link out of the album folder")
        if entry.is_file():
            actual[unicodedata.normalize("NFC", relative)] = entry.stat().st_size
    differences = [f"missing {name}" for name in expected if name not in actual]
    differences += [f"not in the torrent: {name}" for name in actual if name not in expected]
    differences += [
        f"{name} is {actual[name]} bytes, not {size}"
        for name, size in expected.items()
        if name in actual and actual[name] != size
    ]
    if differences:
        more = f" (and {len(differences) - 3} more)" if len(differences) > 3 else ""
        raise CrossUploadRefused(f"the files at {folder} are not the torrent's: {'; '.join(differences[:3])}{more}")
    limit = target.TAG_RULES.max_path_length
    too_long = [name for name in expected if len(f"{folder.name}/{name}") > limit]
    if too_long:
        raise CrossUploadRefused(f"{len(too_long)} path(s) are longer than {target.site_string}'s {limit} characters")


def _track_data(release: Release) -> dict[str, Any]:
    """The files' audio info and tags, read once (read only)."""
    if not release.track_data:
        path = str(release.path)
        release.track_data = concat_track_data(gather_tags(path), gather_audio_info(path))
    return release.track_data


def _conversion_tasks(
    release: Release, transcodes: tuple[str, ...], downconvert: bool, all_formats: bool
) -> list[dict[str, Any]]:
    """The conversions asked for, as the tasks `up` makes for this source.

    Raises:
        CrossUploadRefused: If one is asked for that this source cannot give.
    """
    if not (transcodes or downconvert or all_formats):
        return []
    if release.torrent["format"] != "FLAC":
        raise CrossUploadRefused("only a FLAC can be transcoded or downconverted")
    options = get_downconversion_options({"encoding": release.torrent["encoding"]}, _track_data(release))
    if all_formats:
        return options
    tasks = [option for option in options if option["action"] == "downconvert"] if downconvert else []
    if downconvert and not tasks:
        raise CrossUploadRefused("--downconvert: there is no lossless downconversion of this FLAC")
    tasks += [option for option in options if option["action"] == "transcode" and option["encoding"] in transcodes]
    return tasks


def _tracker_of(url: str, sites: tuple["BaseGazelleApi", ...]) -> str | None:
    """The tracker whose site or image host serves url, or None: one of sites by its origin, or by its hosts."""
    for site in sites:
        if _same_origin(url, site.base_url):
            return site.site_code
    host = (urlparse(url).hostname or "").lower()
    for code, hosts in TRACKER_HOSTS.items():
        if any(host == known or host.endswith(f".{known}") for known in hosts):
            return code
    return None


def _shows_on(tracker: str, target: str) -> bool:
    """Whether an image on tracker's own host shows on target's pages, as its bare URL (cover and descriptions)."""
    if tracker == target:
        return True
    rules = HOST_RULES.get(TRACKER_IMAGE_HOSTS.get(tracker, ""))
    return rules is not None and (rules.displays_on is None or target.lower() in rules.displays_on)


def _bare(url: str) -> str:
    """The URL without its query, fragment or user info: RED signs its image URLs per viewer, with their id."""
    parsed = urlparse(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return parsed._replace(netloc=netloc, query="", fragment="").geturl()


def _images(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Every image the form shows: (field, URL), the cover first."""
    found = [("image", data["image"])] if data.get("image") else []
    for field_name in ("album_desc", "release_desc"):
        for match in _IMAGE_TAG.finditer(data.get(field_name) or ""):
            found.append((field_name, match[1] or match[2]))
    return found


def _images_to_rehost(
    data: dict[str, Any], source: "BaseGazelleApi", target: "BaseGazelleApi"
) -> list[tuple[str, str]]:
    """Decide what happens to each tracker image.

    An image on a host that is no tracker's stays as it is. One on a tracker's host that TARGET shows goes as
    its bare URL (rewritten in data here). One on SOURCE's own site that TARGET cannot show is rehosted.

    Returns:
        The (field, URL) of each image to rehost, in order.

    Raises:
        CrossUploadRefused: For an image no client can fetch, or too many to rehost.
    """
    rehost: list[tuple[str, str]] = []
    for field_name, url in _images(data):
        tracker = _tracker_of(url, (source, target))
        if tracker is None:
            continue
        if _shows_on(tracker, target.site_code):
            data[field_name] = data[field_name].replace(url, _bare(url))
            continue
        if tracker != source.site_code or not _same_origin(url, source.base_url):
            raise CrossUploadRefused(
                f"it shows an image on {urlparse(url).hostname}, which {target.site_string} cannot show"
            )
        rehost.append((field_name, url))
    if len({url for _, url in rehost}) > MAX_REHOSTED_IMAGES:
        raise CrossUploadRefused(f"more than {MAX_REHOSTED_IMAGES} images would need rehosting")
    return rehost


def _same_origin(url: str, base_url: str) -> bool:
    left, right = urlparse(url), urlparse(base_url)
    return (left.scheme, left.hostname, left.port) == (right.scheme, right.hostname, right.port)


async def _check_lossy_approval(release: Release, source: "BaseGazelleApi", target: "BaseGazelleApi") -> None:
    """Decide whether TARGET gets a lossy report after the upload: SOURCE's lossy approval does not carry over.

    Approved on SOURCE (lossy master or lossy WEB): the report goes, with the user's comment if they give one.
    Unknown: the user is asked, and no report goes by default. A cross-upload makes no spectrals: when the torrent
    description shows some, the comment says so by default; otherwise the user is told the report should say where
    TARGET's staff can check.
    """
    page = f"{source.base_url}/torrents.php?torrentid={release.torrent['id']}"
    approved = await _lossy_approval(release, source)
    if approved is None:
        click.secho(
            f"Could not tell whether {source.site_string} approved torrent {release.torrent['id']} as lossy master or "
            f"lossy WEB.",
            fg="yellow",
        )
        if cfg.upload.yes_all:
            click.secho(
                f"Not reporting it as lossy on {target.site_string}: check {page}, and if it is approved, report the "
                f"new torrent on {target.site_string} by hand.",
                fg="yellow",
            )
            release.notes.append(f"lossy approval unknown on {source.site_string}: no lossy report, check {page}")
            return
        approved = click.confirm(
            click.style(f"Report it as lossy on {target.site_string} after the upload?", fg="magenta"), default=False
        )
    if not approved:
        return
    spectrals = _shows_spectrals(release.data["release_desc"])
    if not spectrals:
        click.secho(
            f"The torrent description shows no spectrals, so the lossy report on {target.site_string} should say "
            "where its staff can check the files.",
            fg="yellow",
        )
    comment = SPECTRALS_COMMENT if spectrals else ""
    if not cfg.upload.yes_all:
        comment = await click.prompt(
            click.style(
                f"Comment for the lossy report on {target.site_string} (it already links the torrent on "
                f"{source.site_string})",
                fg="cyan",
                bold=True,
            ),
            default=comment,
            show_default=spectrals,
        )
    note = f"Approved as lossy on {source.site_string}: {page}"
    release.lossy_report = f"{comment}\n\n{note}" if comment else note
    release.notes.append(
        f"approved as lossy on {source.site_string}: a lossy report goes to {target.site_string} after the upload"
    )


def _shows_spectrals(description: str) -> bool:
    """Whether a torrent description shows spectrals: it names them and has an image."""
    return "spectral" in description.lower() and _IMAGE_TAG.search(description) is not None


async def _lossy_approval(release: Release, source: "BaseGazelleApi") -> bool | None:
    """Whether SOURCE approved the torrent as lossy master or lossy WEB, or None when it cannot be told."""
    torrent = release.torrent
    keys = ("lossyMasterApproved", "lossyWebApproved")
    if any(key in torrent for key in keys):
        return any(torrent.get(key) for key in keys)
    if source.site_code != "OPS" or not source.has_session_cookie:
        return None
    # OPS's API does not say: its group page does, as a label on the torrent's row. The page holds the
    # user's passkey and authkey: it is only parsed, never printed or kept.
    try:
        page = await source._request("GET", f"{source.base_url}/torrents.php", params={"id": release.group["id"]})
    except RequestFailedError:
        # An answer about this group: no page to read the label from. Any other failure is SOURCE's own and
        # stops the run.
        return None
    return lossy_label(page.text, int(torrent["id"]))


def lossy_label(page: str, torrent_id: int) -> bool | None:
    """Whether the group page labels the torrent's row lossy master or lossy WEB approved; None without the row."""
    try:
        row = BeautifulSoup(page, "lxml").find("tr", id=f"torrent{torrent_id}")
    except Exception:
        return None
    if row is None or "torrent_row" not in (row.get("class") or []):
        return None
    for tag in row.find_all(True):
        classes = tag.get("class") or []
        title = str(tag.get("title") or "").strip().lower()
        if any(name in classes for name in _LOSSY_CLASSES) or title in _LOSSY_TITLES:
            return True
        if tag.name == "strong" and tag.get_text(strip=True).lower() in _LOSSY_TITLES:
            return True
    return False


def most_requests(release: Release, target: "BaseGazelleApi", group_id: int | None) -> tuple[int, int, int]:
    """The most requests uploading one release can send, retries aside: GETs and POSTs to TARGET, GETs to SOURCE.

    TARGET's index call, once per run, is not counted.
    """
    data = release.data
    uploads = 1 + len(release.tasks)
    if group_id is not None:
        gets = 1  # The group, to confirm it
    else:
        gets = len(_searchstrs(data)) + 1  # The search, and a group pasted at the prompt
        if cfg.upload.requests.check_recent_uploads and target.has_session_cookie:
            gets += 9  # The site log, read when the search finds nothing
    if release.tasks:
        gets += 1  # The group, for the formats it already has
    if target.site_code == "RED":
        gets += uploads  # RED's upload page, for an upload into an existing group through it
    gets += 2  # Looking up an upload whose answer was lost (then the run stops)
    posts = uploads
    if release.lossy_report is not None:
        posts += uploads  # The lossy report of each upload, never sent again
        gets += 2 * uploads  # The pages each redirects to
    return gets, posts, len({url for _, url in release.rehost})


def _searchstrs(data: dict[str, Any]) -> list[str]:
    """What `up` searches TARGET for: the main artists with the title, and the catalogue number."""
    main = [
        (name, "main")
        for name, importance in zip(data["artists[]"], data["importance[]"], strict=True)
        if importance == ARTIST_IMPORTANCES["main"]
    ]
    return generate_dupe_check_searchstrs(main, data["title"], data["remaster_catalogue_number"])


def _print_plan(
    releases: list[Release], source: "BaseGazelleApi", target: "BaseGazelleApi", group_id: int | None
) -> None:
    click.secho(f"\nThe plan: {source.site_string} to {target.site_string}", fg="cyan", bold=True)
    for number, release in enumerate(releases, 1):
        data, torrent = release.data, release.torrent
        artists = ", ".join(data["artists[]"][:3]) + (" ..." if len(data["artists[]"]) > 3 else "")
        formats = [f"{data['format']} {data['bitrate']}", *(task["name"] for task in release.tasks)]
        # The dupe check finds the group on the group's year: say it when the edition's differs.
        years = data["remaster_year"]
        if str(data["year"]) != str(data["remaster_year"]):
            years = f"group {data['year']}, edition {data['remaster_year']}"
        click.echo(f"{number}. {artists} - {data['title']} ({years}), {data['media']}")
        click.echo(f"   from {source.base_url}/torrents.php?torrentid={torrent['id']}")
        click.echo(f"   files: {release.path}")
        click.echo(f"   uploads: {', '.join(formats)}")
        gets, posts, image_gets = most_requests(release, target, group_id)
        if release.rehost:
            click.echo(f"   images to rehost: {image_gets}, each fetched once from {source.site_string}")
        for note in release.notes:
            click.echo(f"   {note}")
        click.echo(
            f"   at most {gets} GET and {posts} POST to {target.site_string}, {image_gets} GET to {source.site_string}"
        )
    if cfg.upload.torrent_name_normalization not in ("", "none"):
        click.secho(
            "torrent_name_normalization is not applied: each new torrent names the files as they are on disk, so it "
            "seeds from them.",
            fg="yellow",
        )


# Phase D: TARGET.


async def _upload(
    release: Release,
    source: "BaseGazelleApi",
    target: "BaseGazelleApi",
    seedbox: UploadManager,
    uploaded: list[str],
    *,
    group_id: int | None,
) -> None:
    """Dupe-check one release on TARGET, upload it, then its conversions. Adds each URL to uploaded.

    Raises:
        RequestError, UploadError, click.Abort, CrossUploadRefused: To stop the run.
    """
    data = dict(release.data)
    click.secho(f"\nCross-uploading {data['title']} to {target.site_string}", fg="cyan", bold=True)
    edition = _edition(release)
    if group_id is not None:
        group_id = (
            group_id if await _confirm_group_id(target, group_id, [], offer_deletion=False, release=edition) else None
        )
    else:
        group_id = await check_existing_group(
            target, _searchstrs(data), offer_deletion=False, our_title=data["title"], release=edition
        )
    existing_group = group_id is not None

    await _rehost_images(data, release.rehost, source, target)
    _refuse_secrets(data, source)
    if group_id is not None:
        data["groupid"] = group_id

    await target.ensure_authenticated()
    torrent_path, torrent = generate_torrent(target, str(release.path), normalize=False)
    files = await compile_files(str(release.path), torrent, {"source": release.torrent["media"]})
    if not dryrun.active():
        click.secho(f"Uploading {release.path.name} to {target.site_string}...", fg="yellow")
    torrent_id, group_id = await target.upload(dict(data), files)
    if release.lossy_report is not None:
        # Sent once; a report TARGET does not take is printed for reporting by hand, and the upload stands.
        await report_lossy_master(target, torrent_id, None, None, data["media"], release.lossy_report)
    url = finish_upload(
        target, str(release.path), torrent_id, torrent_path, torrent, data["format"], seedbox, copy_folder=False
    )
    uploaded.append(url)
    if seedbox.tasks and release.path.parent != Path(cfg.directory.download_directory).resolve():
        click.secho(
            f"The files are in {release.path.parent}: check that the seedbox's torrent client seeds from there.",
            fg="yellow",
        )

    if release.tasks:
        await _upload_conversions(release, target, group_id, torrent_id, url, existing_group, seedbox, uploaded)


def _edition(release: Release) -> dict[str, Any]:
    """The release as master's dupe check and converters take it: artists with roles, edition, media, format."""
    data = release.data
    roles = {importance: role for role, importance in ARTIST_IMPORTANCES.items()}
    return {
        "artists": [
            (name, roles[importance]) for name, importance in zip(data["artists[]"], data["importance[]"], strict=True)
        ],
        "title": data["title"],
        # The edition's year, as up's metadata has it; group_year is the original release's.
        "year": data["remaster_year"],
        "group_year": data["year"],
        "edition_title": data["remaster_title"] or None,
        "label": data["remaster_record_label"],
        "catno": data["remaster_catalogue_number"],
        "source": data["media"],
        "format": data["format"],
        "encoding": data["bitrate"],
        "encoding_vbr": data["vbr"],
        "scene": bool(data.get("scene")),
        "urls": [],
    }


async def _rehost_images(
    data: dict[str, Any], rehost: list[tuple[str, str]], source: "BaseGazelleApi", target: "BaseGazelleApi"
) -> None:
    """Fetch each SOURCE image TARGET cannot show through SOURCE's client, upload it to TARGET's host, swap the URL.

    Each URL is fetched once, even when it is both the cover and a description image going to two hosts.

    Raises:
        CrossUploadRefused: If a fetch or an upload fails, or a host gives no usable URL.
    """
    images: dict[str, tuple[bytes, str]] = {}
    new_urls: dict[tuple[str, str], str] = {}
    for field_name, url in rehost:
        host = (
            cfg.image.host_for(target.site_code, "cover_uploader")
            if field_name == "image"
            else image_host_for_tracker(target.site_code)
        )
        if url not in images:
            images[url] = await _fetch_source_image(url, source)
        key = (host, url)
        if key not in new_urls:
            new_urls[key] = await _upload_image(*images[url], _bare(url), target, host)
        data[field_name] = data[field_name].replace(url, new_urls[key])


async def _fetch_source_image(url: str, source: "BaseGazelleApi") -> tuple[bytes, str]:
    """Fetch an image from SOURCE's own site through SOURCE's client: its content and its file suffix.

    Raises:
        CrossUploadRefused: If it is not on SOURCE's site, cannot be fetched, or is not an image.
    """
    shown = _bare(url)  # The URL may carry SOURCE's per-viewer signature: never printed.
    if not _same_origin(url, source.base_url):
        raise CrossUploadRefused(f"{shown} is not on {source.site_string}'s own site")
    click.secho(f"Fetching {shown} from {source.site_string}...", fg="yellow")
    try:
        # Through SOURCE's client: its rate limit, retries and redirect rules, and its credentials only ever
        # go to its own site.
        response = await source._request(
            "GET", url, timeout_secs=30, prefer_api_key=not source.has_session_cookie, needs_authkey=False, binary=True
        )
    except RequestError as error:
        raise CrossUploadRefused(f"could not fetch {shown} ({type(error).__name__})") from None
    suffix = next((ext for magic, ext in _IMAGE_MAGIC.items() if response.content.startswith(magic)), None)
    if suffix is None and response.content[:4] == b"RIFF" and response.content[8:12] == b"WEBP":
        suffix = ".webp"
    if suffix is None:
        raise CrossUploadRefused(f"{shown} is not an image")
    return response.content, suffix


async def _upload_image(content: bytes, suffix: str, shown: str, target: "BaseGazelleApi", host: str) -> str:
    """Upload an image fetched from SOURCE (shown as its bare URL) to one of TARGET's hosts, and give its URL.

    Raises:
        CrossUploadRefused: If the upload fails or the host gives no usable URL.
    """
    click.secho(f"Rehosting {shown} to {host}...", fg="yellow")
    with TemporaryDirectory() as directory:
        image_path = Path(directory) / f"image{suffix}"
        await anyio.Path(image_path).write_bytes(content)
        if dryrun.active():
            dryrun.say(f"not uploading {shown} to {host}.")
            return dryrun.image_url(str(image_path), host)
        try:
            async with red_api_for_covers(target, host) as red_api:
                uploader = red_images.ImageUploader(red_api) if host == "red" else HOSTS[host].ImageUploader()
                new_url, _ = await uploader.upload_file(str(image_path))
        except (ImageUploadFailed, ValueError) as error:
            raise CrossUploadRefused(f"could not upload {shown} to {host}: {error}") from None
    parsed = urlparse(new_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CrossUploadRefused(f"{host} gave no usable URL for {shown}")
    return new_url


def _refuse_secrets(data: dict[str, Any], source: "BaseGazelleApi") -> None:
    """Make sure nothing in the form carries one of SOURCE's secrets (session, API key, authkey, passkey).

    Raises:
        CrossUploadRefused: Without naming the secret.
    """
    secrets = [secret for secret in source._secrets() if secret and len(secret) >= 8]
    for name, value in data.items():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and any(secret in item for secret in secrets):
                raise CrossUploadRefused(f"the {name} field would carry a {source.site_string} credential")


async def _upload_conversions(
    release: Release,
    target: "BaseGazelleApi",
    group_id: int,
    torrent_id: int,
    url: str,
    existing_group: bool,
    seedbox: UploadManager,
    uploaded: list[str],
) -> None:
    """Upload the conversions asked for that the group does not have yet, with master's converters and forms."""
    edition = _edition(release)
    tasks = release.tasks
    if existing_group:
        group = await target.torrentgroup(group_id)
        formats = {task["name"]: downconversion_format(task) for task in tasks}
        held = held_formats(group, edition, {"id": torrent_id}, formats)
        for name in sorted(held):
            click.secho(f"Not uploading {name}: the group already has it in this edition.", fg="yellow")
        tasks = [task for task in tasks if task["name"] not in held]
    if not tasks:
        return
    await execute_downconversion_tasks(
        tasks,
        str(release.path),
        target,
        group_id,
        edition,
        cover_url=None,
        track_data=_track_data(release),
        hybrid=False,
        # As `up` does, each conversion of a torrent reported as lossy is reported too, quoting that report.
        lossy_master=release.lossy_report is not None,
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=release.lossy_report,
        request_id=None,
        source_url=None,
        seedbox_uploader=seedbox,
        source=release.torrent["media"],
        base_url=url,
        uploaded=uploaded,
    )
