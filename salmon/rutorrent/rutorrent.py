#!/usr/bin/env python3
import xmlrpc.client


def add_torrent_to_rutorrent(server_url, torrent_path, directory, label):
    client = xmlrpc.client.Server(server_url)
    with open(torrent_path, 'rb') as torrent_file:
        torrent_bin = xmlrpc.client.Binary(torrent_file.read())
        if label:
            client.load.raw_start_verbose(
                '', torrent_bin, 'print=d.hash=',
                'd.directory.set=' + directory,
                'd.custom1.set=' + label
            )
        else:
            client.load.raw_start_verbose(
                '', torrent_bin, 'print=d.hash=',
                'd.directory.set=' + directory
            )
