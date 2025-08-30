import argparse
import collections
import os
import posixpath
import subprocess
from concurrent.futures import ThreadPoolExecutor

import click
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from salmon import cfg
from salmon.uploader.torrent_client import TorrentClientGenerator


class Uploader:
    def __init__(self, url, extra_args, client):
        self.url = url
        self.extra_args = extra_args
        self.client = TorrentClientGenerator.parse_libtc_url(client)

    def upload_folder(self, remote_folder, path, type):
        raise NotImplementedError

    def add_to_downloader(self, remote_folder, path, type, label):
        raise NotImplementedError


# Deprecated
class WebDAVUploader(Uploader):
    def upload_file(self, local_path, remote_path):
        with open(local_path, "rb") as file:
            session = requests.Session()
            retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
            session.mount("http://", HTTPAdapter(max_retries=retries))

            try:
                response = session.put(remote_path.replace("\\", "/"), data=file)
                response.raise_for_status()
                click.secho(f"Upload successful: {local_path} to {remote_path}", fg="green")
            except requests.exceptions.RequestException as err:
                click.secho(f"Upload failed: {local_path}, Error: {err}", fg="red")

    def upload_folder(self, remote_folder, path, type):
        click.secho(f"Starting WebDAV upload to {self.url}{remote_folder}", fg="cyan")
        if type == "folder":
            files_to_upload = []
            click.secho(f"Scanning folder: {path}", fg="yellow")

            for root, _dirs, files in os.walk(path):
                for name in files:
                    local_path = os.path.join(root, name)
                    remote_path = self.url + posixpath.join(
                        remote_folder,
                        os.path.relpath(local_path, start=os.path.dirname(path)),
                    )
                    files_to_upload.append((local_path, remote_path))

            click.secho(f"Found {len(files_to_upload)} files to upload", fg="yellow")
            with ThreadPoolExecutor(max_workers=8) as executor:
                for local_path, remote_path in files_to_upload:
                    executor.submit(self.upload_file, local_path, remote_path)
            click.secho("WebDAV folder upload completed", fg="green")
        elif type == "seed":
            remote_path = self.url + os.path.join(remote_folder, os.path.basename(path))
            self.upload_file(path, remote_path)
            click.secho("WebDAV seed file upload completed", fg="green")
        else:
            click.secho("Unsupported upload type", fg="red")
            raise ValueError("Unsupported upload type")


class RcloneUploader(Uploader):
    def upload_folder(self, remote_folder, path, type):
        click.secho(f"Starting Rclone upload to {self.url}:{remote_folder}", fg="cyan")
        remote_path = posixpath.join(remote_folder, os.path.basename(path))
        commands = [
            "rclone",
            "copy",
            path,
            f"{self.url}:{remote_path}",
            *self.extra_args,
        ]

        click.secho(f"Executing: {' '.join(commands)}", fg="yellow")

        result = subprocess.run(commands)

        if result.returncode == 0:
            click.secho(f"Rclone upload successful: {path} to {self.url}:{remote_path}", fg="green")
        else:
            click.secho(f"Rclone upload failed with exit code {result.returncode}", fg="red")

    def add_to_downloader(self, remote_folder, path, type, label):
        click.secho(f"Adding torrent to client: {os.path.basename(path)}", fg="cyan")
        with open(path, "rb") as file:
            torrent = file.read()

        parser = argparse.ArgumentParser()
        parser.add_argument("--sftp-path-override", type=str, default=None)
        known_args, _ = parser.parse_known_args(self.extra_args)
        path_override = known_args.sftp_path_override
        shell_path = remote_folder

        if path_override:
            shell_path = path_override
            if path_override[0] == "@":
                shell_path = posixpath.join(path_override.removeprefix("@"), remote_folder.removeprefix("/"))

        try:
            self.client.add_to_downloader(shell_path, torrent, is_paused=False, label=label)
            click.secho("Torrent added to client successfully", fg="green")
        except Exception as e:
            click.secho(f"Failed to add torrent to client: {e}", fg="red")


class LocalUploader(Uploader):
    def upload_folder(self, remote_folder, path, type):
        click.secho("Skipping upload for local mode (no transfer needed)", fg="yellow")
        return

    def add_to_downloader(self, remote_folder, path, type, label):
        click.secho(f"Adding torrent to local client: {os.path.basename(path)}", fg="cyan")
        with open(path, "rb") as file:
            torrent = file.read()

        try:
            download_path = remote_folder if remote_folder else os.path.abspath(cfg.directory.download_directory)
            self.client.add_to_downloader(download_path, torrent, is_paused=False, label=label)
            click.secho("Torrent added to local client successfully", fg="green")
        except Exception as e:
            click.secho(f"Failed to add torrent to local client: {e}", fg="red")


class UploaderGenerator:
    @staticmethod
    def get_uploader(type, url, extra_args, torrent_client):
        if type == "webdav":
            return WebDAVUploader(url, extra_args, torrent_client)
        elif type == "rclone":
            return RcloneUploader(url, extra_args, torrent_client)
        elif type == "local":
            return LocalUploader(url, extra_args, torrent_client)
        else:
            click.secho(f"Unsupported uploader type: {type}", fg="red")
            raise ValueError("Unsupported uploader type")


class UploadManager:
    def __init__(self):
        self.uploaders = []
        self.tasks = collections.deque()
        self._generate_uploaders()

    def _generate_uploaders(self):
        click.secho("Initializing upload managers", fg="cyan")
        for seedbox in cfg.seedbox:
            try:
                uploader = UploaderGenerator.get_uploader(
                    seedbox.type, seedbox.url, seedbox.extra_args, seedbox.torrent_client
                )
                self.uploaders.append(
                    {
                        "uploader": uploader,
                        "directory": seedbox.directory,
                        "flac_only": seedbox.flac_only,
                        "label": seedbox.label,
                    }
                )
                click.secho(f"Configured {seedbox.type} uploader to {seedbox.url}", fg="yellow")
            except Exception as e:
                click.secho(f"Failed to configure {seedbox.type} uploader: {e}", fg="red")

    def add_upload_task(self, directory, task_type, is_flac=True):
        click.secho(f"Preparing upload tasks for: {directory}", fg="cyan")
        for uploader_info in self.uploaders:
            remote_directory = uploader_info.get("directory")
            label = uploader_info.get("label")

            current_task = (
                uploader_info["uploader"],
                remote_directory,
                directory,
                task_type,
                label,
            )

            if (current_task not in self.tasks) and (is_flac or (not uploader_info["flac_only"])):
                if task_type == "seed":
                    self.tasks.append(current_task)
                    click.secho(f"Added seed task to {uploader_info['uploader'].__class__.__name__}", fg="magenta")
                elif task_type == "folder":
                    self.tasks.appendleft(current_task)
                    click.secho(
                        f"Added folder transfer task to {uploader_info['uploader'].__class__.__name__}", fg="magenta"
                    )

    def execute_upload(self):
        if not self.tasks:
            click.secho("No upload tasks to execute", fg="yellow")
            return

        click.secho(f"Executing {len(self.tasks)} upload tasks", fg="cyan")
        for i, task in enumerate(self.tasks, 1):
            uploader, remote_directory, local_directory, task_type, label = task
            try:
                click.secho(
                    f"\nTask {i}/{len(self.tasks)}: {task_type.upper()} - {os.path.basename(local_directory)}",
                    fg="cyan",
                )
                if task_type == "folder":
                    uploader.upload_folder(remote_directory, local_directory, task_type)
                elif task_type == "seed":
                    uploader.add_to_downloader(remote_directory, local_directory, task_type, label)
            except Exception as e:
                click.secho(f"Critical error during task: {e}", fg="red")

        click.secho("\nAll upload tasks processed", fg="green")
        self.tasks.clear()
