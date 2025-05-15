import subprocess
import threading
from salmon import config
from salmon.uploader.torrent_client import TorrentClientGenerator
from concurrent.futures import ThreadPoolExecutor
import collections
import asyncio
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.auth import HTTPBasicAuth
import os
import posixpath
import pathlib
import json
import argparse


class Uploader:
    def __init__(self, url, username, password, extra_args, client):
        self.url = url
        self.username = username
        self.password = password
        self.extra_args = extra_args
        self.client = TorrentClientGenerator.parse_libtc_url(client)

    def upload_folder(self, remote_folder, path, type):
        raise NotImplementedError

    def add_to_downloader(self, remote_folder, path, type):
        raise NotImplementedError


# Deprecated
class WebDAVUploader(Uploader):
    def upload_file(self, local_path, remote_path):
        with open(local_path, "rb") as file:
            session = requests.Session()
            retries = Retry(
                total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504]
            )
            session.mount('http://', HTTPAdapter(max_retries=retries))

            try:
                response = session.put(
                    remote_path.replace('\\', '/'),
                    data=file,
                    auth=HTTPBasicAuth(self.username, self.password),
                )
                response.raise_for_status()
                print(f"Upload successful: {local_path} to {remote_path}")
            except requests.exceptions.RequestException as err:
                print(f"Upload failed: {local_path}, Error: {err}")

    def upload_folder(self, remote_folder, path, type):
        if type == 'folder':
            files_to_upload = []

            for root, dirs, files in os.walk(path):
                for name in files:
                    local_path = os.path.join(root, name)
                    remote_path = self.url + posixpath.join(
                        remote_folder,
                        os.path.relpath(local_path, start=os.path.dirname(path)),
                    )
                    files_to_upload.append((local_path, remote_path))

            with ThreadPoolExecutor(max_workers=8) as executor:
                for local_path, remote_path in files_to_upload:
                    executor.submit(self.upload_file, local_path, remote_path)
        elif type == 'seed':
            remote_path = self.url + os.path.join(remote_folder, os.path.basename(path))
            self.upload_file(path, remote_path)
        else:
            raise ValueError("Unsupported upload type")

class RcloneUploader(Uploader):
    def upload_folder(self, remote_folder, path, type):
        remote_path = posixpath.join(remote_folder, os.path.basename(path))
        if os.path.exists('config/rclone.conf'):
            config_args = ["--config", 'config/rclone.conf']
        else:
            config_args = []
        commands = [
            'rclone',
            *config_args,
            'copy',
            path,
            f'{self.url}:{remote_path}',
            *self.extra_args,
        ]
        subprocess.run(commands)

    def add_to_downloader(self, remote_folder, path, type):
        with open(path, 'rb') as file:
            torrent = file.read()

        parser = argparse.ArgumentParser()
        parser.add_argument("--sftp-path-override", type=str, default=None)
        known_args, _ = parser.parse_known_args(self.extra_args)
        path_override = known_args.sftp_path_override
        shell_path = remote_folder

        if path_override:
            shell_path = path_override
            if path_override[0] == '@':
                shell_path = posixpath.join(
                    path_override.removeprefix('@'), remote_folder.removeprefix('/')
                )

        self.client.add_to_downloader(
            shell_path, torrent, is_paused=False, label="smoked-salmon"
        )


class LocalUploader(Uploader):
    def upload_folder(self, remote_folder, path, type):
        return

    def add_to_downloader(self, remote_folder, path, type):
        with open(path, 'rb') as file:
            torrent = file.read()

        self.client.add_to_downloader(
            config.DOWNLOAD_DIRECTORY, torrent, is_paused=False, label="smoked-salmon"
        )


class UploaderGenerator:
    @staticmethod
    def get_uploader(type, url, username, password, extra_args, torrent_client):
        if type == 'webdav':
            return WebDAVUploader(url, username, password)
        elif type == 'rclone':
            return RcloneUploader(url, username, password, extra_args, torrent_client)
        elif type == 'local':
            return LocalUploader(url, username, password, extra_args, torrent_client)
        else:
            raise ValueError("Unsupported uploader type")


class UploadManager:
    def __init__(self, config_json):
        self.uploaders = []
        self.tasks = collections.deque()
        self._parse_config(config_json)

    def _parse_config(self, config_json):
        configs = (
            json.loads(open(config_json, 'r').read())
            if os.path.exists(config_json)
            else {}
        )
        for config in configs:
            if config['enabled']:
                uploader = UploaderGenerator.get_uploader(
                    config.get('type'),
                    config.get('url'),
                    config.get('username'),
                    config.get('password'),
                    config.get('extra_args'),
                    config.get('torrent_client'),
                )
                self.uploaders.append(
                    {
                        'uploader': uploader,
                        'directory': config.get('directory'),
                        'watchdir': config.get('watchdir'),
                        'flac_only': config.get('flac_only'),
                    }
                )

    def add_upload_task(self, directory, task_type, is_flac=True):
        for uploader_info in self.uploaders:
            remote_directory = uploader_info.get('directory')

            current_task = (
                uploader_info['uploader'],
                remote_directory,
                directory,
                task_type,
            )

            if current_task not in self.tasks:
                if is_flac or (not uploader_info['flac_only']):
                    if task_type == "seed":
                        self.tasks.append(current_task)
                    elif task_type == "folder":
                        # Prepend file transfers to the front and append downloads to the end, ensuring the downloader always runs after the file transfer
                        self.tasks.appendleft(current_task)

    def execute_upload(self):
        for uploader, remote_directory, local_directory, task_type in self.tasks:
            if task_type == "folder":
                uploader.upload_folder(remote_directory, local_directory, task_type)
                print(f"Upload completed to {remote_directory}")
            elif task_type == "seed":
                uploader.add_to_downloader(remote_directory, local_directory, task_type)
                print(f"Send seed completed to {remote_directory}")
