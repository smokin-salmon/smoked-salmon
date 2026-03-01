import re
from os import path

import asyncclick as click
import requests

from salmon import cfg

LOCAL_VERSION_FILE = path.abspath(path.join(path.dirname(path.dirname(__file__)), "data", "version.py"))
REMOTE_VERSION_URL = "https://raw.githubusercontent.com/smokin-salmon/smoked-salmon/refs/heads/master/data/version.py"


def _extract_changelog(content, from_version, to_version):
    """Extracts the changelog entries between the specified versions."""
    pattern = rf'__version__\s*=\s*"{re.escape(to_version)}"\s*(.*?)__version__\s*=\s*"{re.escape(from_version)}"'
    match = re.search(pattern, content, re.DOTALL)
    return match.group(1).strip() if match else None


def _extract_version(content: str) -> str | None:
    """Extracts the version string from file content."""
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    return match.group(1) if match else None


def _parse_version(ver):
    """Convert a version string into a tuple for comparison, handling pre-release versions."""
    match = re.match(r"(\d+(?:\.\d+)*)-?([a-zA-Z]*)", ver)
    if not match:
        return (0,)
    num_part = tuple(map(int, match.group(1).split(".")))
    suffix = match.group(2)

    # Assign a weight for pre-release tags (lower than final versions)
    suffix_order = {"alpha": -3, "beta": -2, "rc": -1, "": 0}

    return num_part + (suffix_order.get(suffix, -4),)  # Default to lowest priority if unknown


def get_version() -> str | None:
    """Returns the local installed version, or None if not found."""
    try:
        with open(LOCAL_VERSION_FILE, encoding="utf-8") as f:
            content = f.read()
        return _extract_version(content)
    except FileNotFoundError:
        return None


def _get_local_version(version_file):
    """Extracts the local version from the version.py file."""
    try:
        with open(version_file, encoding="utf-8") as f:
            content = f.read()
        version = _extract_version(content)
        if version:
            click.secho(f"Local Version: {version}", fg="yellow")
            return version
        else:
            click.secho("Version not found in local file.", fg="red")
            return None
    except FileNotFoundError:
        click.secho("Version file not found.", fg="red")
        return None


def _get_remote_version(url):
    """Fetches the latest version information from the remote repository."""
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            content = response.text
            version = _extract_version(content)
            if version:
                return version, content
            else:
                click.secho("Version not found in remote file.", fg="red")
                return None, None
        else:
            click.secho(f"Failed to fetch remote version file. Status code: {response.status_code}", fg="red")
            return None, None
    except requests.RequestException as e:
        click.secho(f"An error occurred while fetching the remote version file: {e}", fg="red")
        return None, None


def show_release_notification():
    """Checks for updates and notifies the user."""
    notify = cfg.upload.update_notification
    verbose = cfg.upload.update_notification_verbose

    if not notify:
        return

    local_version = _get_local_version(LOCAL_VERSION_FILE)
    if not local_version:
        return

    remote_version, remote_content = _get_remote_version(REMOTE_VERSION_URL)
    if not remote_version:
        return

    if _parse_version(remote_version) > _parse_version(local_version):
        click.secho(f"[NOTICE] Update available: v{remote_version}", fg="green", bold=True)

        if verbose and remote_content:
            changelog = _extract_changelog(remote_content, local_version, remote_version)
            if changelog:
                click.secho(changelog, fg="yellow")
            else:
                click.secho(f"Changelog not found between versions ({local_version} -> {remote_version}).", fg="yellow")
    else:
        click.secho("No new version available.", fg="green")
