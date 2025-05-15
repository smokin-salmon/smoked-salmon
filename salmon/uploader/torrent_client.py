import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.auth import HTTPBasicAuth
import os
import posixpath
import json
import base64
import transmission_rpc
import qbittorrentapi
from deluge_client import DelugeRPCClient


class TorrentClient:
    def __init__(self, username, password, url=None, scheme=None, host=None, port=None):
        self.username = username
        self.password = password
        self.url = url
        self.scheme = scheme
        self.host = host
        self.port = port

        self.client = self.login()

    def login(self):
        raise NotImplementedError

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        raise NotImplementedError


class QBittorrentClient(TorrentClient):
    def login(self):
        try:
            qbt_client = qbittorrentapi.Client(
                host=self.url,
                username=self.username,
                password=self.password
            )
            qbt_client.auth_log_in()
            return qbt_client

        except qbittorrentapi.LoginFailed:
            print("INCORRECT QBIT LOGIN CREDENTIALS")
            return None
        except qbittorrentapi.APIConnectionError:
            print("APIConnectionError: INCORRECT HOST/PORT")
            return None

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None
        try:
            self.client.torrents_add(
                torrent_files=torrent,
                save_path=remote_folder,
                is_paused=is_paused,
                category=label
            )
        except qbittorrentapi.APIConnectionError as e:
            print(f"Failed to add torrent: {e}")
            return


class TransmissionClient(TorrentClient):
    def login(self):
        try:
            trt = transmission_rpc.Client(
                protocol=self.scheme,
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=60,
            )
            return trt
        except Exception as err:
            print(f"Connect to Transmission failed: {str(err)}")
            return None

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None
        try:
            return self.client.add_torrent(
                torrent=torrent,
                download_dir=remote_folder,
                paused=True,
                labels=[label],
            )
        except Exception as err:
            print(f"Failed to add torrent: {str(err)}")
            return None


class DelugeClient(TorrentClient):
    def login(self):
        try:
            de_client = DelugeRPCClient(host=self.host, port=self.port, username=self.username, password=self.password)
            de_client.connect()
            if de_client.connected is True:
                return de_client
            else:
                return None
        except Exception as err:
            print(f"Connect to Deluge failed: {str(err)}")
            return None
        

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None
        self.client.call('core.add_torrent_file', f"{os.urandom(16).hex()}.torrent", base64.b64encode(torrent), {'download_location': remote_folder})


TORRENT_CLIENT_MAPPING = {
    "deluge": DelugeClient,
    "transmission": TransmissionClient,
    "qbittorrent": QBittorrentClient,
}


class TorrentClientGenerator:
    @staticmethod
    def parse_libtc_url(url):
        # transmission+http://127.0.0.1:9091/?session_path=/session/path/
        # rtorrent+scgi:///path/to/socket.scgi?session_path=/session/path/
        # deluge://username:password@127.0.0.1:58664/?session_path=/session/path/
        # qbittorrent+http://username:password@127.0.0.1:8080/?session_path=/session/path/

        kwargs = {}
        parsed = urlparse(url)
        scheme = parsed.scheme.split("+")
        netloc = parsed.netloc
        if "@" in netloc:
            auth, netloc = netloc.split("@")
            username, password = auth.split(":")
            kwargs["username"] = username
            kwargs["password"] = password

        client = scheme[0]
        if client in ["qbittorrent"]:
            kwargs["url"] = f"{scheme[1]}://{netloc}{parsed.path}"
        else:
            kwargs['scheme'] = scheme[1]
            kwargs["host"], kwargs["port"] = netloc.split(":")
            kwargs["port"] = int(kwargs["port"])

        return TORRENT_CLIENT_MAPPING[client](**kwargs)
