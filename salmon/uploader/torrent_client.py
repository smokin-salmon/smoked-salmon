import base64
import os
import xmlrpc.client
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
            result = self.client.call(
                "core.add_torrent_file",
                f"{torrent_id}.torrent",
                base64.b64encode(torrent),
                {"download_location": remote_folder, "add_paused": is_paused},
            )

            # Set label if provided
            if label and result:
                try:
                    click.secho(f"Setting label '{label}' for torrent...", fg="yellow")
                    self.client.call("label.set_torrent", result, label)
                except Exception as label_error:
                    # If setting label failed, try to add the label first
                    if "Unknown Label" in str(label_error) or "label does not exist" in str(label_error).lower():
                        try:
                            click.secho(f"Creating label '{label}'...", fg="yellow")
                            self.client.call("label.add", label)
                            # Try setting the label again
                            self.client.call("label.set_torrent", result, label)
                            click.secho(f"Label '{label}' set successfully", fg="green")
                        except Exception as add_label_error:
                            click.secho(f"Failed to create/set label: {add_label_error}", fg="red")
                    else:
                        click.secho(f"Failed to set label: {label_error}", fg="red")
                else:
                    click.secho(f"Label '{label}' set successfully", fg="green")

            click.secho("Torrent added successfully", fg="green")
            return result
        except Exception as e:
            click.secho(f"Failed to add torrent: {e}", fg="red", bold=True)
            return None


class RuTorrentClient(TorrentClient):
    def login(self):
        try:
            rt_client = xmlrpc.client.Server(self.url)
            # TODO: Test connection
            return rt_client
        except Exception as e:
            click.secho(f"Connect to ruTorrent failed: {e}", fg="red", bold=True)
            return None

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None

        try:
            click.secho("Adding torrent to ruTorrent...", fg="yellow")
            torrent_bin = xmlrpc.client.Binary(torrent)
            commands = [
                "print=d.hash=",
                f"d.directory.set={remote_folder}",
                *([f"d.custom1.set={label}"] if label else []),
            ]

            if is_paused:
                self.client.load.raw_verbose("", torrent_bin, *commands)
            else:
                self.client.load.raw_start_verbose("", torrent_bin, *commands)

            click.secho("Torrent added successfully", fg="green")
        except Exception as e:
            click.secho(f"Failed to add torrent: {e}", fg="red", bold=True)


TORRENT_CLIENT_MAPPING = {
    "deluge": DelugeClient,
    "transmission": TransmissionClient,
    "qbittorrent": QBittorrentClient,
    "rutorrent": RuTorrentClient,
}


class TorrentClientGenerator:
    @staticmethod
    def parse_libtc_url(url):
        click.secho(f"\nParsing torrent client URL: {url}", fg="cyan")

        # transmission+http://127.0.0.1:9091
        # rutorrent+http://RUTORRENT_ADDRESS:9380/plugins/rpc/rpc.php
        # deluge://username:password@127.0.0.1:58664
        # qbittorrent+http://username:password@127.0.0.1:8080

        kwargs = {}
        parsed = urlparse(url)
        scheme = parsed.scheme.split("+")
        netloc = parsed.netloc
        if "@" in netloc:
            auth, netloc = netloc.rsplit("@", 1)
            username, password = auth.split(":", 1)
            kwargs["username"] = username
            kwargs["password"] = password

        client = scheme[0]
        if client in ["qbittorrent"]:
            kwargs["url"] = f"{scheme[1]}://{netloc}{parsed.path}"
        elif client in ["rutorrent"]:
            kwargs["url"] = netloc
        else:
            kwargs["scheme"] = scheme[1]
            kwargs["host"], kwargs["port"] = netloc.split(":")
            kwargs["port"] = int(kwargs["port"])

        return TORRENT_CLIENT_MAPPING[client](**kwargs)
