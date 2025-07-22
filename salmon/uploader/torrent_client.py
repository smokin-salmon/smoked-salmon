import base64
import os
from urllib.parse import urlparse

import click
import qbittorrentapi
import transmission_rpc
from deluge_client import DelugeRPCClient


class TorrentClient:
    def __init__(self, username, password, url=None, scheme=None, host=None, port=None):
        self.username = username
        self.password = password
        self.url = url
        self.scheme = scheme
        self.host = host
        self.port = port

        click.secho(f"Initializing {self.__class__.__name__} client...", fg="cyan")
        self.client = self.login()

    def login(self):
        raise NotImplementedError

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        raise NotImplementedError


class QBittorrentClient(TorrentClient):
    def login(self):
        try:
            click.secho("Attempting to connect to qBittorrent...", fg="yellow")
            qbt_client = qbittorrentapi.Client(host=self.url, username=self.username, password=self.password)
            qbt_client.auth_log_in()
            
            click.secho("Successfully connected to qBittorrent", fg="green")
            return qbt_client

        except qbittorrentapi.LoginFailed:
            click.secho("INCORRECT QBIT LOGIN CREDENTIALS", fg="red", bold=True)
            return None
        except qbittorrentapi.APIConnectionError:
            click.secho("APIConnectionError: Incorrect host or port", fg="red", bold=True)
            return None

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None
            
        try:
            click.secho("Adding torrent to qBittorrent...", fg="yellow")
            self.client.torrents_add(
                torrent_files=torrent, save_path=remote_folder, is_paused=is_paused, category=label
            )
            click.secho("Torrent added successfully", fg="green")
        except Exception as e:
            click.secho(f"Failed to add torrent: {e}", fg="red", bold=True)
            return


class TransmissionClient(TorrentClient):
    def login(self):
        try:
            click.secho("Attempting to connect to Transmission...", fg="yellow")
            trt = transmission_rpc.Client(
                protocol=self.scheme,
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=60,
            )
            click.secho("Successfully connected to Transmission", fg="green")
            return trt
        except Exception as e:
            click.secho(f"Connect to Transmission failed: {e}", fg="red", bold=True)
            return None

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None
            
        try:
            click.secho("Adding torrent to Transmission...", fg="yellow")
            result = self.client.add_torrent(
                torrent=torrent,
                download_dir=remote_folder,
                paused=is_paused,
                labels=[label],
            )
            click.secho("Torrent added successfully", fg="green")
            return result
        except Exception as e:
            click.secho(f"Failed to add torrent: {e}", fg="red", bold=True)
            return None


class DelugeClient(TorrentClient):
    def login(self):
        try:
            click.secho("Attempting to connect to Deluge...", fg="yellow")
            de_client = DelugeRPCClient(host=self.host, port=self.port, username=self.username, password=self.password)
            de_client.connect()
            if de_client.connected is True:
                click.secho("Successfully connected to Deluge", fg="green")
                return de_client
            else:
                click.secho("Deluge connection failed: Not connected", fg="red", bold=True)
                return None
        except Exception as e:
            click.secho(f"Connect to Deluge failed: {e}", fg="red", bold=True)
            return None

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None
            
        try:
            click.secho("Adding torrent to Deluge...", fg="yellow")
            torrent_id = os.urandom(16).hex()
            self.client.call(
                "core.add_torrent_file",
                f"{torrent_id}.torrent",
                base64.b64encode(torrent),
                {"download_location": remote_folder},
            )
            click.secho("Torrent added successfully", fg="green")
        except Exception as e:
            click.secho(f"Failed to add torrent: {e}", fg="red", bold=True)


TORRENT_CLIENT_MAPPING = {
    "deluge": DelugeClient,
    "transmission": TransmissionClient,
    "qbittorrent": QBittorrentClient
}


class TorrentClientGenerator:
    @staticmethod
    def parse_libtc_url(url):
        click.secho(f"\nParsing torrent client URL: {url}", fg="cyan")
        
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
            kwargs["scheme"] = scheme[1]
            kwargs["host"], kwargs["port"] = netloc.split(":")
            kwargs["port"] = int(kwargs["port"])

        return TORRENT_CLIENT_MAPPING[client](**kwargs)
