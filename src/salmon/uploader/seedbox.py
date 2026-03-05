import argparse
import collections
import os
import posixpath

import anyio
import asyncclick as click

from salmon import cfg
from salmon.config.validations import Seedbox
from salmon.uploader.torrent_client import TorrentClient, TorrentClientGenerator


def _resolve_shell_path(remote_folder: str, extra_args: list[str]) -> str:
    """Resolve the effective download path, respecting --sftp-path-override.

    Args:
        remote_folder: The base remote directory path.
        extra_args: Extra CLI arguments that may contain --sftp-path-override.

    Returns:
        Effective shell path for the torrent client.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--sftp-path-override", type=str, default=None)
    known_args, _ = parser.parse_known_args(extra_args)
    override = known_args.sftp_path_override
    if not override:
        return remote_folder
    if override.startswith("@"):
        return posixpath.join(override.removeprefix("@"), remote_folder.removeprefix("/"))
    return override


async def _rclone_upload_folder(seedbox: Seedbox, remote_folder: str, path: str) -> None:
    """Upload a local folder to a rclone remote.

    Args:
        seedbox: Seedbox config providing the rclone remote URL and extra args.
        remote_folder: Destination directory on the remote.
        path: Local folder path to upload.
    """
    remote_path = posixpath.join(remote_folder, os.path.basename(path))
    commands = ["rclone", "copy", path, f"{seedbox.url}:{remote_path}", *seedbox.extra_args]
    click.secho(f"Starting Rclone upload to {seedbox.url}:{remote_folder}", fg="cyan")
    click.secho(f"Executing: {' '.join(commands)}", fg="yellow")
    result = await anyio.run_process(commands)
    if result.returncode == 0:
        click.secho(f"Rclone upload successful: {path} to {seedbox.url}:{remote_path}", fg="green")
    else:
        click.secho(f"Rclone upload failed with exit code {result.returncode}", fg="red")


async def _add_to_downloader(
    client: TorrentClient,
    shell_path: str,
    torrent_path: str,
    label: str,
    add_paused: bool,
) -> None:
    """Read a torrent file and add it to the download client.

    Args:
        client: Torrent client instance.
        shell_path: Download directory path passed to the client.
        torrent_path: Local path to the .torrent file.
        label: Label to apply in the download client.
        add_paused: Whether to add the torrent in paused state.
    """
    async with await anyio.open_file(torrent_path, "rb") as f:
        torrent = await f.read()
    try:
        client.add_to_downloader(shell_path, torrent, is_paused=add_paused, label=label)
        click.secho("Torrent added to client successfully", fg="green")
    except Exception as e:
        click.secho(f"Failed to add torrent to client: {e}", fg="red")


class UploadManager:
    """Collects upload and seed tasks during a session and executes them all at once.

    Folder tasks are prepended to the queue (run first) so files are present
    on the remote before the corresponding torrents are added to the client.
    """

    def __init__(self) -> None:
        click.secho("Initializing upload managers", fg="cyan")
        self._client_cache: dict[str, TorrentClient] = {}
        for seedbox in cfg.seedbox:
            try:
                if seedbox.torrent_client not in self._client_cache:
                    self._client_cache[seedbox.torrent_client] = TorrentClientGenerator.parse_libtc_url(
                        seedbox.torrent_client
                    )
                click.secho(f"Configured {seedbox.type} uploader to {seedbox.url}", fg="yellow")
            except Exception as e:
                click.secho(f"Failed to configure {seedbox.type} uploader: {e}", fg="red")

        # Each task: (seedbox, local_path, task_type)
        self.tasks: collections.deque[tuple[Seedbox, str, str]] = collections.deque()

    def _client(self, seedbox: Seedbox) -> TorrentClient:
        """Look up the cached torrent client for a seedbox entry.

        Args:
            seedbox: Seedbox config whose torrent_client URL is used as the cache key.

        Returns:
            The cached TorrentClient instance.
        """
        return self._client_cache[seedbox.torrent_client]

    def add_upload_task(self, directory: str, task_type: str, is_flac: bool) -> None:
        """Queue upload tasks for a path across all configured seedboxes.

        Args:
            directory: Local folder path (for "folder" tasks) or .torrent file path (for "seed" tasks).
            task_type: Either "folder" to transfer files or "seed" to add to the download client.
            is_flac: Whether the release is FLAC; skips seedboxes with flac_only=True if False.
        """
        click.secho(f"Preparing upload tasks for: {directory}", fg="cyan")
        for seedbox in cfg.seedbox:
            if seedbox.torrent_client not in self._client_cache:
                continue
            if seedbox.flac_only and not is_flac:
                continue
            task = (seedbox, directory, task_type)
            if task in self.tasks:
                continue
            if task_type == "seed":
                self.tasks.append(task)
                click.secho("Added seed task", fg="magenta")
            elif task_type == "folder":
                self.tasks.appendleft(task)
                click.secho("Added folder transfer task", fg="magenta")

    async def execute_upload(self) -> None:
        """Execute all queued upload tasks in order (folders first, then seeds)."""
        if not self.tasks:
            click.secho("No upload tasks to execute", fg="yellow")
            return

        click.secho(f"Executing {len(self.tasks)} upload tasks", fg="cyan")
        for i, (seedbox, local_path, task_type) in enumerate(self.tasks, 1):
            click.secho(
                f"\nTask {i}/{len(self.tasks)}: {task_type.upper()} - {os.path.basename(local_path)}",
                fg="cyan",
            )
            try:
                if task_type == "folder":
                    if seedbox.type == "rclone":
                        await _rclone_upload_folder(seedbox, seedbox.directory, local_path)
                elif task_type == "seed":
                    client = self._client(seedbox)
                    if seedbox.type == "rclone":
                        shell_path = _resolve_shell_path(seedbox.directory, seedbox.extra_args)
                    else:
                        shell_path = seedbox.directory or os.path.abspath(cfg.directory.download_directory)
                    await _add_to_downloader(client, shell_path, local_path, seedbox.label, seedbox.add_paused)
            except Exception as e:
                click.secho(f"Critical error during task: {e}", fg="red")

        click.secho("\nAll upload tasks processed", fg="green")
        self.tasks.clear()
