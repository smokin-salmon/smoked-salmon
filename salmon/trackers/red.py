import asyncio

from salmon import cfg
from salmon.errors import (
    RequestError,
)
from salmon.trackers.base import BaseGazelleApi

loop = asyncio.get_event_loop()


class RedApi(BaseGazelleApi):
    def __init__(self):
        self.site_code = 'RED'
        self.base_url = 'https://redacted.sh'
        self.tracker_url = 'https://flacsfor.me'
        self.site_string = 'RED'
        if cfg.tracker.red:
            red_cfg = cfg.tracker.red

            self.cookie = red_cfg.session
            if red_cfg.api_key:
                self.api_key = red_cfg.api_key

            if red_cfg.dottorrents_dir:
                self.dot_torrents_dir = red_cfg.dottorrents_dir
            else:
                self.dot_torrents_dir = cfg.directory.dottorrents_dir

        super().__init__()

    async def report_lossy_master(self, torrent_id, comment, source):
        """Automagically report a torrent for lossy master/web approval.
        Use LWA if the torrent is web, otherwise LMA."""

        url = self.base_url + "/reportsv2.php"
        params = {"action": "takereport"}
        type_ = "lossywebapproval" if source == "WEB" else "lossyapproval"
        data = {
            "auth": self.authkey,
            "torrentid": torrent_id,
            "categoryid": 1,
            "type": type_,
            "extra": comment,
            "submit": True,
        }
        r = await loop.run_in_executor(
            None,
            lambda: self.session.post(
                url, params=params, data=data, headers=self.headers
            ),
        )
        if "torrents.php" in r.url:
            return True
        raise RequestError(
            f"Failed to report the torrent for lossy master, code {r.status_code}."
        )
