from pathlib import Path

import asyncclick as click
import msgspec
import requests
from packaging.version import Version

from salmon import cfg

LOCAL_VERSION_FILE = Path(__file__).parent / "data" / "version.toml"
REMOTE_VERSION_URL = (
    "https://raw.githubusercontent.com/"
    "tomerh2001/smoked-salmon/refs/heads/master/src/salmon/data/version.toml"
)

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

    remote_data = _get_remote_version_data(REMOTE_VERSION_URL)
    if not remote_data:
        return

    if Version(remote_data.current) > Version(local_version):
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
