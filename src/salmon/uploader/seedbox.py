import collections
import dataclasses
import os
import posixpath
import shutil
import subprocess
import sys
import unicodedata

import anyio
import anyio.abc
import asyncclick as click

from salmon import cfg, dryrun
from salmon.common.redaction import redact_command, redact_secrets, secret_values, url_secrets
from salmon.config.validations import Seedbox
from salmon.errors import DryRunRefused
from salmon.uploader.torrent_client import TorrentClient, TorrentClientGenerator
from salmon.webui import output

# A path override shorter than this is not masked: it would match inside every path in the output.
_MIN_MASKED_PATH = 4


def _path_override(extra_args: list[str]) -> str | None:
    """The last --sftp-path-override value in extra_args, as rclone reads it; None without one."""
    override = None
    i = 0
    while i < len(extra_args):
        name, equals, value = extra_args[i].partition("=")
        i += 1
        if name == "--sftp-path-override":
            if equals:
                override = value
            else:
                # The next argument is this flag's value, whatever it looks like.
                override = extra_args[i] if i < len(extra_args) else None
                i += 1
    return override


def _resolve_shell_path(remote_folder: str, extra_args: list[str]) -> str:
    """Resolve the effective download path, respecting --sftp-path-override.

    Args:
        remote_folder: The base remote directory path.
        extra_args: Extra CLI arguments that may contain --sftp-path-override.

    Returns:
        Effective shell path for the torrent client.
    """
    override = _path_override(extra_args)
    if not override:
        return remote_folder
    if override.startswith("@"):
        return posixpath.join(override.removeprefix("@"), remote_folder.removeprefix("/"))
    return override


def seedbox_secrets(seedbox: Seedbox) -> list[str]:
    """The secrets a seedbox's rclone remote, extra_args and torrent client URL carry, to mask wherever echoed."""
    secrets = [*secret_values(seedbox.extra_args, seedbox.url), *url_secrets(seedbox.torrent_client)]
    raw = _path_override(seedbox.extra_args)
    if raw:
        override = raw.removeprefix("@")
        if len(override) >= _MIN_MASKED_PATH and override != "/":
            # A home folder names the account; the client gets the path without its "@", and an error can repeat it.
            secrets.append(override)
        else:
            # Masking "/" or a few characters would wreck every path in the output.
            # Only the occurrence the override added: another secret may have the same value.
            if raw in secrets:
                secrets.remove(raw)
    return secrets


async def _rclone_upload_folder(seedbox: Seedbox, remote_folder: str, path: str) -> bool:
    """Upload a local folder to a rclone remote.

    Args:
        seedbox: Seedbox config providing the rclone remote URL and extra args.
        remote_folder: Destination directory on the remote.
        path: Local folder path to upload.

    Returns:
        True if rclone exited successfully, False otherwise.
    """
    folder_name = os.path.basename(path)
    if _normalizes_to_nfc(seedbox):
        # rclone makes every name it copies NFC with the flag, but not this explicit destination name.
        folder_name = unicodedata.normalize("NFC", folder_name)
    remote_path = posixpath.join(remote_folder, folder_name)
    commands = ["rclone", "copy", path, f"{seedbox.url}:{remote_path}", *seedbox.extra_args]
    secrets = seedbox_secrets(seedbox)
    click.secho(redact_secrets(f"Starting Rclone upload to {seedbox.url}:{remote_folder}", secrets), fg="cyan")
    click.secho(f"Executing: {redact_command(commands, secrets)}", fg="yellow")
    returncode = await _run_rclone(commands, secrets)
    if returncode == 0:
        click.secho(
            redact_secrets(f"Rclone upload successful: {path} to {seedbox.url}:{remote_path}", secrets), fg="green"
        )
        return True
    click.secho(f"Rclone upload failed with exit code {returncode}", fg="red")
    return False


async def _run_rclone(commands: list[str], secrets: list[str]) -> int:
    """Run rclone, printing its stderr with the seedbox's secrets masked.

    rclone's stdout goes to the terminal as it is, so -P can render live progress. Its stderr is
    printed a line at a time as it comes: that is where rclone names a remote it could not open
    (a connection string with its password) and, with -vv, its whole command line. With -P, what
    rclone logs once the copy has started goes to stdout, within the progress display, instead.

    In a salmon web job there is no terminal: what rclone writes to its stdout would reach the server's,
    not the job's log. Its stdout is then read too, and printed a line at a time, masked, as its stderr.

    Args:
        commands: The rclone command line.
        secrets: Values to mask in what rclone prints, from seedbox_secrets.

    Returns:
        rclone's exit code.
    """
    in_job = output.in_job()
    stdout = subprocess.PIPE if in_job else None
    async with await anyio.open_process(commands, stdin=None, stdout=stdout, stderr=subprocess.PIPE) as process:
        assert process.stderr is not None
        async with anyio.create_task_group() as task_group:
            if process.stdout is not None:
                task_group.start_soon(_print_rclone_output, process.stdout, secrets, False)
            await _print_rclone_output(process.stderr, secrets, True)
        return await process.wait()


async def _print_rclone_output(stream: anyio.abc.ByteReceiveStream, secrets: list[str], err: bool) -> None:
    """Print what rclone writes to `stream` a line at a time, to salmon's stderr (or stdout), masked."""
    pending = b""
    async for chunk in stream:
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            _print_rclone_line(line, secrets, err)
    if pending:
        _print_rclone_line(pending, secrets, err)


def _print_rclone_line(line: bytes, secrets: list[str], err: bool = True) -> None:
    """Print a line rclone wrote, to salmon's stderr (or stdout), with the seedbox's secrets masked."""
    click.echo(redact_secrets(line.decode(errors="replace").rstrip("\r"), secrets), err=err)


async def _add_to_downloader(
    client: TorrentClient,
    shell_path: str,
    torrent_path: str,
    label: str,
    add_paused: bool,
    secrets: list[str],
    seedbox_name: str,
) -> bool:
    """Read a torrent file and add it to the download client.

    Args:
        client: Torrent client instance.
        shell_path: Download directory path passed to the client.
        torrent_path: Local path to the .torrent file.
        label: Label to apply in the download client.
        add_paused: Whether to add the torrent in paused state.
        secrets: Values to mask in an error, from seedbox_secrets.
        seedbox_name: The seedbox's name (or its masked url) to name in a failure line.

    Returns:
        True if the client reported the torrent was added, False otherwise.
    """
    async with await anyio.open_file(torrent_path, "rb") as f:
        torrent = await f.read()
    try:
        added = client.add_to_downloader(shell_path, torrent, is_paused=add_paused, label=label)
    except Exception as e:
        click.secho(f"Failed to add torrent to client on {seedbox_name}: {redact_secrets(str(e), secrets)}", fg="red")
        return False
    if not added:
        click.secho(f"Torrent was not added to the client on {seedbox_name}", fg="red")
        return False
    click.secho("Torrent added to client successfully", fg="green")
    return True


def _seedbox_display_name(seedbox: Seedbox, secrets: list[str]) -> str:
    """The name to blame in a failure line: the seedbox's `name`, else its masked `url`."""
    if seedbox.name:
        return seedbox.name
    return redact_secrets(seedbox.url, secrets)


def _enabled_seedboxes() -> list[Seedbox]:
    """Return the configured seedboxes that are not disabled with `enabled = false`."""
    return [seedbox for seedbox in cfg.seedbox if seedbox.enabled]


_UNICODE_FLAG = "--local-unicode-normalization"
# rclone reads every flag from its RCLONE_* variable too; a flag on the command line wins over it.
_UNICODE_ENV = "RCLONE_LOCAL_UNICODE_NORMALIZATION"
# The true spellings of Go's strconv.ParseBool, which rclone reads both with.
_GO_TRUE = {"1", "t", "T", "true", "TRUE", "True"}
# Seedboxes already warned about in this process: once is enough for a config problem.
_warned_normalization: set[int] = set()


def _normalizes_to_nfc(seedbox: Seedbox) -> bool:
    """Whether rclone makes the names it copies NFC: the last such flag in extra_args, else its variable."""
    flags = [arg.partition("=") for arg in seedbox.extra_args if arg.partition("=")[0] == _UNICODE_FLAG]
    if not flags:
        # salmon starts rclone with its own environment.
        return os.environ.get(_UNICODE_ENV, "") in _GO_TRUE
    _, has_value, value = flags[-1]
    return not has_value or value in _GO_TRUE


def _unmatched_normalization(seedbox: Seedbox, form: str) -> str | None:
    """Why a seedbox's client may not find files a torrent names in this Unicode form; None if it will."""
    if seedbox.type == "rclone":
        if form == "NFD":
            return "rclone cannot copy names as NFD"
        if not _normalizes_to_nfc(seedbox):
            return f"rclone copies names as they are, without {_UNICODE_FLAG} in its extra_args or {_UNICODE_ENV} set"
        return None
    # macOS finds a file whatever the form of its name; Linux compares the bytes.
    if sys.platform.startswith("linux"):
        return "its client seeds the files where they are, and Linux keeps their names as they are"
    return None


def warn_unmatched_normalization(form: str, site_code: str) -> None:
    """Warn once per seedbox when the client behind it may not find the files a form-normalized torrent names."""
    if not cfg.upload.upload_to_seedbox:
        return
    for seedbox in _enabled_seedboxes():
        if seedbox.trackers and site_code.upper() not in seedbox.trackers:
            continue
        if id(seedbox) in _warned_normalization or not (reason := _unmatched_normalization(seedbox, form)):
            continue
        _warned_normalization.add(id(seedbox))
        click.secho(
            f"\nWarning: torrent_name_normalization is {form}, but on "
            f"{_seedbox_display_name(seedbox, seedbox_secrets(seedbox))} {reason}. Its client cannot seed a file "
            f'whose name is not {form} already: set torrent_name_normalization to "none" unless they all are.',
            fg="yellow",
        )


@dataclasses.dataclass
class _Outcome:
    """One queued task and whether it succeeded: False until it has run and worked."""

    seedbox: Seedbox
    local_path: str
    task_type: str
    folder: str
    ok: bool = False


class UploadManager:
    """Collects upload and seed tasks during a session and executes them all at once.

    Folder tasks are prepended to the queue (run first) so files are present
    on the remote before the corresponding torrents are added to the client.
    """

    def __init__(self) -> None:
        self._client_cache: dict[str, TorrentClient] = {}
        # Each task: (seedbox, local_path, task_type, folder). `folder` is the release folder the
        # task belongs to: itself for a "folder" task, and the folder its torrent was built from
        # for a "seed" task. It lets a failed copy skip only the seeds for its own folder, not
        # every seed queued for that seedbox (a folder task's copy failing must not stop the seed
        # of a different release, or a different format of the same release, on the same seedbox).
        self.tasks: collections.deque[tuple[Seedbox, str, str, str]] = collections.deque()
        # What remove_local_after_copy decides from: every task's outcome (keyed by seedbox identity, path, type
        # and folder), and every .torrent built from each folder, whether or not a seedbox took it.
        self._outcomes: dict[tuple[int, str, str, str], _Outcome] = {}
        self._folder_torrents: dict[str, list[str]] = {}
        if dryrun.active():
            # Setting up a torrent client logs into it. A dry run queues nothing, so it needs none.
            if _enabled_seedboxes():
                dryrun.say("not connecting to the seedboxes' torrent clients.")
            return

        click.secho("Initializing upload managers", fg="cyan")
        for seedbox in _enabled_seedboxes():
            secrets = seedbox_secrets(seedbox)
            try:
                if seedbox.torrent_client not in self._client_cache:
                    self._client_cache[seedbox.torrent_client] = TorrentClientGenerator.parse_libtc_url(
                        seedbox.torrent_client
                    )
                click.secho(
                    redact_secrets(f"Configured {seedbox.type} uploader to {seedbox.url}", secrets), fg="yellow"
                )
            except DryRunRefused:
                # Were the skip above missed, the client's refusal to log in must stop the run, not read as
                # a seedbox that failed to configure.
                raise
            except Exception as e:
                click.secho(f"Failed to configure {seedbox.type} uploader: {redact_secrets(str(e), secrets)}", fg="red")

    def _client(self, seedbox: Seedbox) -> TorrentClient:
        """Look up the cached torrent client for a seedbox entry.

        Args:
            seedbox: Seedbox config whose torrent_client URL is used as the cache key.

        Returns:
            The cached TorrentClient instance.
        """
        return self._client_cache[seedbox.torrent_client]

    def add_upload_task(
        self,
        directory: str,
        task_type: str,
        is_flac: bool,
        folder: str | None = None,
        site_code: str | None = None,
    ) -> None:
        """Queue upload tasks for a path across all configured seedboxes.

        Args:
            directory: Local folder path (for "folder" tasks) or .torrent file path (for "seed" tasks).
            task_type: Either "folder" to transfer files or "seed" to add to the download client.
            is_flac: Whether the release is FLAC; skips seedboxes with flac_only=True if False.
            folder: For a "seed" task (required), the release folder its torrent was built from, so a
                failed copy of that folder skips this seed. Ignored for a "folder" task, which always uses
                its own path.
            site_code: Tracker this upload went to. Skips a seedbox whose `trackers` is set and
                does not contain it; a seedbox with no `trackers` still matches every tracker.

        Raises:
            DryRunRefused: In a dry run, which copies nothing to a seedbox and adds nothing to a client.
            ValueError: For a "seed" task without its `folder`: a failed copy of it could not skip the seed.
        """
        dryrun.refuse(f"queue {directory} for a seedbox copy or a torrent client")
        if task_type == "seed" and not folder:
            raise ValueError("A seed task needs the folder its torrent was built from.")
        click.secho(f"Preparing upload tasks for: {directory}", fg="cyan")
        task_folder = folder if task_type == "seed" and folder else directory
        torrents = self._folder_torrents.setdefault(task_folder, [])
        if task_type == "seed" and directory not in torrents:
            torrents.append(directory)
        for seedbox in _enabled_seedboxes():
            if seedbox.torrent_client not in self._client_cache:
                continue
            if seedbox.flac_only and not is_flac:
                continue
            if seedbox.trackers and (site_code or "").upper() not in seedbox.trackers:
                continue
            task = (seedbox, directory, task_type, task_folder)
            if task in self.tasks:
                continue
            self._outcomes[(id(seedbox), directory, task_type, task_folder)] = _Outcome(
                seedbox, directory, task_type, task_folder
            )
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
        # Keyed by (id(seedbox), folder): a failed copy of one folder must not skip the seed of a
        # different folder queued for the same seedbox.
        failed_folders: set[tuple[int, str]] = set()
        failed_copies = 0
        failed_seeds = 0
        for i, (seedbox, local_path, task_type, folder) in enumerate(self.tasks, 1):
            click.secho(
                f"\nTask {i}/{len(self.tasks)}: {task_type.upper()} - {os.path.basename(local_path)}",
                fg="cyan",
            )
            secrets = seedbox_secrets(seedbox)
            try:
                outcome = self._outcomes.get((id(seedbox), local_path, task_type, folder))
                if task_type == "folder":
                    if seedbox.type == "rclone":
                        succeeded = await _rclone_upload_folder(seedbox, seedbox.directory, local_path)
                        if not succeeded:
                            failed_folders.add((id(seedbox), folder))
                            failed_copies += 1
                        elif outcome:
                            outcome.ok = True
                    elif outcome:
                        # A local seedbox copies nothing.
                        outcome.ok = True
                elif task_type == "seed":
                    if (id(seedbox), folder) in failed_folders:
                        click.secho(
                            redact_secrets(
                                f"Skipping seed on {_seedbox_display_name(seedbox, secrets)}: "
                                f"the Rclone upload of {folder} failed, "
                                f"so {local_path} was not added to the client there. Add it by hand once "
                                "the files have been copied.",
                                secrets,
                            ),
                            fg="red",
                        )
                        failed_seeds += 1
                        continue
                    client = self._client(seedbox)
                    if seedbox.type == "rclone":
                        shell_path = _resolve_shell_path(seedbox.directory, seedbox.extra_args)
                    else:
                        shell_path = seedbox.directory or os.path.abspath(cfg.directory.download_directory)
                    added = await _add_to_downloader(
                        client,
                        shell_path,
                        local_path,
                        seedbox.label,
                        seedbox.add_paused,
                        secrets,
                        _seedbox_display_name(seedbox, secrets),
                    )
                    if not added:
                        failed_seeds += 1
                    elif outcome:
                        outcome.ok = True
            except Exception as e:
                click.secho(f"Critical error during task: {redact_secrets(str(e), secrets)}", fg="red")
                if task_type == "folder":
                    # A folder task that raised (rclone missing, unexpected I/O error, ...) is a
                    # failed copy too: the seed task must not add a torrent for files that may not
                    # be on the remote.
                    failed_folders.add((id(seedbox), folder))
                    failed_copies += 1
                elif task_type == "seed":
                    failed_seeds += 1

        if failed_copies or failed_seeds:
            parts = []
            if failed_copies:
                parts.append(f"{failed_copies} {'copy' if failed_copies == 1 else 'copies'}")
            if failed_seeds:
                parts.append(f"{failed_seeds} seed task{'' if failed_seeds == 1 else 's'}")
            click.secho(f"\n{' and '.join(parts)} failed; see above", fg="red")
        else:
            click.secho("\nAll upload tasks processed", fg="green")
        self.tasks.clear()

    def _kept_reason(self, folder: str) -> str | None:
        """Why the local copy of a folder must stay, or None when every seedbox has it and seeds it."""
        download_directory = os.path.realpath(cfg.directory.download_directory)
        real = os.path.realpath(folder)
        try:
            inside = real != download_directory and os.path.commonpath([download_directory, real]) == download_directory
        except ValueError:  # Different drives on Windows.
            inside = False
        if not inside or not os.path.isdir(real) or os.path.islink(folder):
            return "it is not a folder inside download_directory"
        if cfg.directory.protects(folder):
            return "it is a protected folder"

        outcomes = [o for o in self._outcomes.values() if o.folder == folder]
        # "Took it": queued a copy of it. Whether the copy worked is checked below, so a failure names itself.
        if not any(
            o.seedbox.remove_local_after_copy and o.seedbox.type == "rclone" and o.task_type == "folder"
            for o in outcomes
        ):
            return "no seedbox set to remove the local copy took it"
        for outcome in outcomes:
            name = _seedbox_display_name(outcome.seedbox, seedbox_secrets(outcome.seedbox))
            if not outcome.ok and outcome.task_type == "folder":
                return f"the copy to {name} failed"
            if not outcome.ok:
                return f"{name} could not add {os.path.basename(outcome.local_path)}"
        for outcome in outcomes:
            if outcome.seedbox.type == "local" and outcome.task_type == "seed":
                name = _seedbox_display_name(outcome.seedbox, seedbox_secrets(outcome.seedbox))
                return f"{name} seeds it from download_directory"
        for torrent in self._folder_torrents.get(folder, []):
            if not any(o.ok and o.task_type == "seed" and o.local_path == torrent for o in outcomes):
                return f"{os.path.basename(torrent)} seeds on no seedbox"
        for seedbox in _enabled_seedboxes():
            if seedbox.torrent_client not in self._client_cache:
                return f"{_seedbox_display_name(seedbox, seedbox_secrets(seedbox))} was not configured"
        return None

    def remove_local_copies(self) -> None:
        """Remove each folder from download_directory that remove_local_after_copy allows, and report each one.

        Called by `salmon up` after execute_upload, never by cross-upload. A folder goes only when every task for
        it on every seedbox worked and nothing seeds it from the local copy (see _kept_reason); a dry run queues
        nothing, so it removes nothing.
        """
        if dryrun.active() or not any(
            seedbox.remove_local_after_copy and seedbox.type == "rclone" for seedbox in _enabled_seedboxes()
        ):
            return
        for folder in self._folder_torrents:
            reason = self._kept_reason(folder)
            if reason:
                click.secho(f"remove_local_after_copy is on, but {folder} is kept: {reason}.", fg="yellow")
                continue
            names = sorted(
                {
                    _seedbox_display_name(o.seedbox, seedbox_secrets(o.seedbox))
                    for o in self._outcomes.values()
                    if o.folder == folder and o.task_type == "folder" and o.seedbox.type == "rclone"
                }
            )
            try:
                shutil.rmtree(folder)
            except OSError as e:
                click.secho(f"remove_local_after_copy is on, but {folder} is kept: {e}.", fg="yellow")
                continue
            click.secho(
                f"Removed {folder} from download_directory: copied to and seeding on {', '.join(names)} "
                "(remove_local_after_copy).",
                fg="yellow",
            )
