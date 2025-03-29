import re

from bs4 import BeautifulSoup

from salmon import config
from salmon.trackers.base import BaseGazelleApi


class OpsApi(BaseGazelleApi):
    def __init__(self):
        self.site_code = 'OPS'
        self.base_url = 'https://orpheus.network'
        self.tracker_url = 'https://home.opsfet.ch'
        self.site_string = 'OPS'
        if config.OPS_DOTTORRENTS_DIR:
            self.dot_torrents_dir = config.OPS_DOTTORRENTS_DIR
        else:
            self.dot_torrents_dir = config.DOTTORRENTS_DIR

        self.cookie = config.OPS_SESSION
        if config.OPS_API_KEY:
            self.api_key = config.OPS_API_KEY

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
