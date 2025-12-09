import re

import aiohttp
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
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a", title="Permalink"):
            match = re.search(r"torrents.php\?id=(\d+)\&torrentid=(\d+)", pl["href"])
            if match:
                ids.append((match[2], match[1]))
        return max(ids)

    async def report_lossy_master(self, torrent_id: int, comment: str, source: str) -> bool:
        """Report torrent for lossy master approval (OPS-specific).

        OPS only uses 'lossyapproval' type, not 'lossywebapproval'.

        Args:
            torrent_id: The torrent ID to report.
            comment: The report comment.
            source: Media source.

        Returns:
            True if report was successful.

        Raises:
            RequestError: If the report fails.
        """
        await self.ensure_authenticated()
        url = self.base_url + "/reportsv2.php"
        params = {"action": "takereport"}
        # OPS only uses lossyapproval, not lossywebapproval
        type_ = "lossyapproval"
        data = {
            "auth": self.authkey,
            "torrentid": torrent_id,
            "categoryid": 1,
            "type": type_,
            "extra": comment,
            "submit": True,
        }

        timeout = aiohttp.ClientTimeout(total=10)
        async with (
            aiohttp.ClientSession(timeout=timeout, cookies=self._get_cookies()) as session,
            session.post(url, params=params, data=data, headers=self.headers) as r,
        ):
            resp_url = str(r.url)
            if "torrents.php" in resp_url:
                return True
            raise RequestError(f"Failed to report the torrent for lossy master, code {r.status}.")
