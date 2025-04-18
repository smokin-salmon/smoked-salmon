#!/usr/bin/env python3
import qbittorrentapi


def add_torrent_to_qbittorrent(
    host,
    port,
    username,
    password,
    torrent_path,
    save_path=None,
    category=None,
    paused=False,
    skip_checking=False
):
    try:
        conn_info = {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
        }

        client = qbittorrentapi.Client(**conn_info)
        client.auth_log_in()

        options = {}
        if save_path:
            options["savepath"] = save_path
        if category:
            options["category"] = category
        if paused:
            options["paused"] = "true"
        if skip_checking:
            options["skip_checking"] = "true"

        with open(torrent_path, 'rb') as torrent_file:
            result = client.torrents_add(torrent_files=torrent_file, **options)

        if result == "Ok.":
            print(f"Torrent added successfully to qBittorrent at {host}:{port}")
            return True
        else:
            print(f"Failed to add torrent: {result}")
            return False

    except qbittorrentapi.exceptions.LoginFailed:
        print("Login failed. Check your username and password.")
        return False
    except qbittorrentapi.exceptions.APIConnectionError:
        print(f"Connection error. Make sure qBittorrent is running at {host}:{port} with WebUI enabled.")
        return False
    except Exception as e:
        print(f"Error adding torrent: {str(e)}")
        return False
    finally:
        if 'client' in locals():
            client.auth_log_out()