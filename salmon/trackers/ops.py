import asyncio
import re

from bs4 import BeautifulSoup

from salmon import cfg
from salmon.errors import (
    RequestError,
)
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

        # OPS-specific release types
        self.release_types = {
            "Album": 1,
            "Soundtrack": 3,
            "EP": 5,
            "Anthology": 6,
            "Compilation": 7,
            "Single": 9,
            "Demo": 10,
            "Live album": 11,
            "Split": 12,
            "Remix": 13,
            "Bootleg": 14,
            "Interview": 15,
            "Mixtape": 16,
            "DJ Mix": 17,
            "Concert Recording": 18,
            "Unknown": 21,
        }

    def parse_most_recent_torrent_and_group_id_from_group_page(self, text):
        """
        Given the HTML (ew) response from a successful upload, find the most
        recently uploaded torrent (it better be ours).
        """
        ids = []
        soup = BeautifulSoup(text, "html.parser")
        for pl in soup.find_all("a", title="Permalink"):
            match = re.search(r"torrents.php\?id=(\d+)\&torrentid=(\d+)", pl["href"])
            if match:
                ids.append((match[2], match[1]))
        return max(ids)

    async def report_lossy_master(self, torrent_id, comment, source):
        """Automagically report a torrent for lossy master/web approval."""

        url = self.base_url + "/reportsv2.php"
        params = {"action": "takereport"}
        type_ = "lossyapproval"
        data = {
            "auth": self.authkey,
            "torrentid": torrent_id,
            "categoryid": 1,
            "type": type_,
            "extra": comment,
            "submit": True,
        }
        r = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self.session.post(url, params=params, data=data, headers=self.headers),
        )
        if "torrents.php" in r.url:
            return True
        raise RequestError(f"Failed to report the torrent for lossy master, code {r.status_code}.")
