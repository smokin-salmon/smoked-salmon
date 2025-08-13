import re

from bs4 import BeautifulSoup

from salmon import cfg
from salmon.trackers.base import BaseGazelleApi


class OpsApi(BaseGazelleApi):
    def __init__(self):
        self.site_code = "OPS"
        self.base_url = "https://orpheus.network"
        self.tracker_url = "https://home.opsfet.ch"
        self.site_string = "OPS"
        if cfg.tracker.ops:
            ops_cfg = cfg.tracker.ops
            if ops_cfg.dottorrents_dir:
                self.dot_torrents_dir = ops_cfg.dottorrents_dir
            else:
                self.dot_torrents_dir = cfg.directory.dottorrents_dir

            self.cookie = ops_cfg.session
            if ops_cfg.api_key:
                self.api_key = ops_cfg.api_key

        super().__init__()

    def parse_most_recent_torrent_and_group_id_from_group_page(self, text):
        """
        Given the HTML (ew) response from a successful upload, find the most
        recently uploaded torrent (it better be ours).
        """
        torrent_ids = []
        soup = BeautifulSoup(text, "html.parser")
        for pl in soup.find_all("a", class_="tooltip"):
            torrent_url = re.search(r"torrents.php\?id=(\d+)", pl["href"])
            if torrent_url:
                torrent_ids.append(int(torrent_url[1]))
        return max(torrent_ids)
