from os import path

import asyncclick as click
import msgspec
import requests
from packaging.version import Version

from salmon import cfg

LOCAL_VERSION_FILE = path.abspath(path.join(path.dirname(path.dirname(__file__)), "data", "version.toml"))
REMOTE_VERSION_URL = "https://raw.githubusercontent.com/smokin-salmon/smoked-salmon/refs/heads/master/data/version.toml"


class ChangelogEntry(msgspec.Struct, frozen=True):
    version: str
    notes: str
    date: str

    @property
    def formatted(self) -> str:
        """Formats the entry as a human-readable changelog block.

        Returns:
            A string with a header line followed by the stripped notes content.
        """
        return f"Changelog for version {self.version} ({self.date}):\n{self.notes.strip()}"


class VersionData(msgspec.Struct, frozen=True):
    current: str
    changelog: list[ChangelogEntry]


def _extract_changelog(data: VersionData, from_version: str, to_version: str) -> str | None:
    """Extracts changelog entries between two versions.

    Args:
        data: Parsed version data containing the changelog list.
        from_version: The lower bound version (exclusive).
        to_version: The upper bound version (inclusive).

    Returns:
        A formatted string of changelog entries, or None if no entries found.
    """
    collecting = False
    notes_parts = []
    for entry in data.changelog:
        if entry.version == to_version:
            collecting = True
        if entry.version == from_version:
            break
        if collecting:
            notes_parts.append(entry.formatted)
    return "\n\n".join(notes_parts) if notes_parts else None


def get_version() -> str | None:
    """Returns the local installed version.

    Returns:
        The current version string, or None if the version file is not found.
    """
    try:
        with open(LOCAL_VERSION_FILE, "rb") as f:
            return msgspec.toml.decode(f.read(), type=VersionData).current
    except FileNotFoundError:
        return None


def _get_local_version(version_file: str) -> VersionData | None:
    """Loads and parses the local version file.

    Args:
        version_file: Absolute path to the local version TOML file.

    Returns:
        Parsed VersionData, or None if the file is not found.
    """
    try:
        with open(version_file, "rb") as f:
            data = msgspec.toml.decode(f.read(), type=VersionData)
        click.secho(f"Local Version: {data.current}", fg="yellow")
        return data
    except FileNotFoundError:
        click.secho("Version file not found.", fg="red")
        return None


def _get_remote_version(url: str) -> VersionData | None:
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

    local_data = _get_local_version(LOCAL_VERSION_FILE)
    if not local_data:
        return

    remote_data = _get_remote_version(REMOTE_VERSION_URL)
    if not remote_data:
        return

    if Version(remote_data.current) > Version(local_data.current):
        click.secho(f"[NOTICE] Update available: v{remote_data.current}", fg="green", bold=True)

        if verbose:
            changelog = _extract_changelog(remote_data, local_data.current, remote_data.current)
            if changelog:
                click.secho(changelog, fg="yellow")
            else:
                click.secho(
                    f"Changelog not found between versions ({local_data.current} -> {remote_data.current}).",
                    fg="yellow",
                )
    else:
        click.secho("No new version available.", fg="green")
