import os
import re
from pathlib import Path

import asyncclick as click
import msgspec
import requests
from packaging.version import Version

from salmon import cfg

DISPLAY_VERSION_ENV = "SALMON_DISPLAY_VERSION"
LOCAL_VERSION_FILE = Path(__file__).parent / "data" / "version.toml"
REMOTE_VERSION_URL = (
    "https://raw.githubusercontent.com/"
    "tomerh2001/smoked-salmon/refs/heads/master/src/salmon/data/version.toml"
)
PERSONAL_FORK_RELEASES_URL = "https://api.github.com/repos/tomerh2001/smoked-salmon/releases?per_page=20"
PERSONAL_FORK_VERSION_RE = re.compile(r"^(?P<base>\d+\.\d+\.\d+)-personal-fork\.(?P<run>\d+)$")

_cached_version: str | None = None


class ChangelogEntry(msgspec.Struct, frozen=True):
    version: str
    notes: str
    date: str

    @property
    def header(self) -> str:
        """Returns the changelog entry header line.

        Returns:
            A formatted header string with version and date.
        """
        return f"Changelog for version {self.version} ({self.date}):"


class VersionData(msgspec.Struct, frozen=True):
    current: str
    changelog: list[ChangelogEntry]


def _parse_personal_fork_version(version: str) -> tuple[Version, int] | None:
    """Parse personal-fork release tags into comparable parts."""
    match = PERSONAL_FORK_VERSION_RE.fullmatch(version)
    if match is None:
        return None
    return Version(match.group("base")), int(match.group("run"))


def _personal_fork_sort_key(version: str) -> tuple[Version, int]:
    """Return a non-optional sort key for known personal-fork versions."""
    parsed = _parse_personal_fork_version(version)
    if parsed is None:
        raise ValueError(f"Not a personal-fork version: {version}")
    return parsed


def _extract_changelog(data: VersionData, from_version: str, to_version: str) -> list[ChangelogEntry]:
    """Extracts changelog entries between two versions.

    Args:
        data: Parsed version data containing the changelog list.
        from_version: The lower bound version (exclusive).
        to_version: The upper bound version (inclusive).

    Returns:
        A list of ChangelogEntry objects.
    """
    collecting = False
    entries = []
    for entry in data.changelog:
        if entry.version == to_version:
            collecting = True
        if entry.version == from_version:
            break
        if collecting:
            entries.append(entry)
    return entries


def get_version() -> str | None:
    """Returns the local installed version, cached after first read.

    Returns:
        The current version string, or None if the version file is not found.
    """
    global _cached_version
    if _cached_version is not None:
        return _cached_version
    if display_version := os.environ.get(DISPLAY_VERSION_ENV):
        _cached_version = display_version
        return _cached_version
    try:
        _cached_version = msgspec.toml.decode(LOCAL_VERSION_FILE.read_bytes(), type=VersionData).current
        return _cached_version
    except FileNotFoundError:
        return None


def _get_remote_version_data(url: str) -> VersionData | None:
    """Fetches and parses the remote version file.

    Args:
        url: URL of the remote version TOML file.

    Returns:
        Parsed VersionData, or None if the request fails or returns a non-200 status.
    """
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return msgspec.toml.decode(response.content, type=VersionData)
        else:
            click.secho(f"Failed to fetch remote version file. Status code: {response.status_code}", fg="red")
            return None
    except requests.RequestException as e:
        click.secho(f"An error occurred while fetching the remote version file: {e}", fg="red")
        return None


def _get_remote_personal_fork_version_data(url: str) -> VersionData | None:
    """Fetch the latest personal-fork prerelease metadata from GitHub releases."""
    try:
        response = requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if response.status_code != 200:
            click.secho(f"Failed to fetch personal-fork releases. Status code: {response.status_code}", fg="red")
            return None

        entries: list[ChangelogEntry] = []
        for release in response.json():
            tag_name = release.get("tag_name")
            if not isinstance(tag_name, str):
                continue
            if _parse_personal_fork_version(tag_name) is None:
                continue

            published_at = release.get("published_at") or release.get("created_at") or ""
            notes = release.get("body") or ""
            entries.append(
                ChangelogEntry(
                    version=tag_name,
                    date=published_at[:10] if isinstance(published_at, str) else "",
                    notes=notes if isinstance(notes, str) else "",
                )
            )

        if not entries:
            click.secho("No personal-fork prereleases were found on GitHub.", fg="red")
            return None

        entries.sort(key=lambda entry: _personal_fork_sort_key(entry.version), reverse=True)
        return VersionData(current=entries[0].version, changelog=entries)
    except requests.RequestException as e:
        click.secho(f"An error occurred while fetching personal-fork releases: {e}", fg="red")
        return None


def _is_newer_version(remote_version: str, local_version: str) -> bool:
    """Compare stable and personal-fork version strings safely."""
    remote_fork = _parse_personal_fork_version(remote_version)
    local_fork = _parse_personal_fork_version(local_version)
    if remote_fork and local_fork:
        return remote_fork > local_fork
    return Version(remote_version) > Version(local_version)


def show_release_notification() -> None:
    """Checks for a newer remote version and notifies the user if one is available.

    Reads update_notification and update_notification_verbose from config.
    If a newer version exists, prints a notice and optionally the changelog.
    Does nothing if update_notification is disabled in config.
    """
    notify = cfg.upload.update_notification
    verbose = cfg.upload.update_notification_verbose

    if not notify:
        return

    local_version = get_version()
    if not local_version:
        click.secho("Version file not found.", fg="red")
        return

    click.secho(f"Local Version: {local_version}", fg="yellow")

    if _parse_personal_fork_version(local_version):
        remote_data = _get_remote_personal_fork_version_data(PERSONAL_FORK_RELEASES_URL)
    else:
        remote_data = _get_remote_version_data(REMOTE_VERSION_URL)
    if not remote_data:
        return

    if _is_newer_version(remote_data.current, local_version):
        click.secho(f"[NOTICE] Update available: v{remote_data.current}\n", fg="green", bold=True)

        if verbose:
            changelog = _extract_changelog(remote_data, local_version, remote_data.current)
            if changelog:
                for entry in changelog:
                    click.secho(f"{entry.header}\n", fg="yellow")
                    click.secho(f"{entry.notes.strip()}\n")
            else:
                click.secho(
                    f"Changelog not found between versions ({local_version} -> {remote_data.current}).",
                    fg="yellow",
                )
    else:
        click.secho("No new version available.", fg="green")
