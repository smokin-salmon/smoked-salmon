import re

import asyncclick as click
from bs4 import BeautifulSoup

from salmon import cfg
from salmon.common import UploadFiles
from salmon.constants import ARTIST_IMPORTANCES
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

        self._split_prompted = False
        self._use_split = False

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

    async def upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent, prompting once to set release type to Split for multi-artist releases.

        If the upload is for a new group (has 'releasetype' in data) and two or more
        main artists are listed, the user is asked once whether to set the release type
        to Split (12). Guest-only additions should not trigger this prompt. The answer is
        cached for subsequent uploads in the same session.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).
        """
        main_artist_count = sum(
            1
            for importance in data.get("importance[]", [])
            if str(importance).strip() == str(ARTIST_IMPORTANCES["main"])
        )
        if "releasetype" in data and main_artist_count >= 2 and not self._split_prompted:
            self._use_split = click.confirm(
                click.style(
                    f"\nThis release has {main_artist_count} main artists. "
                    "OPS has a 'Split' release type:\n\n"
                    "An album or EP that includes tracks by two or more separate artists. "
                    "Unlike a compilation or collaboration, a split generally includes several tracks by each artist. "
                    "Also unlike a compilation, a split is made up of new material or new performances, "
                    "not re-packaged tracks from previous albums or singles.\n"
                    "Split albums and EPs often include the word 'split' in the title or on the packaging. "
                    "If you aren't sure if an album is a split, check Discogs, MusicBrainz or another reputable "
                    "source before uploading.\n\n"
                    "Do you want to set the release type to Split?",
                    fg="magenta",
                    bold=True,
                ),
                default=False,
            )
            self._split_prompted = True

        upload_data = (
            {**data, "releasetype": self.release_types["Split"]} if self._use_split and "releasetype" in data else data
        )
        return await super().upload(upload_data, files)

    def parse_most_recent_torrent_and_group_id_from_group_page(self, text: str) -> tuple[int, int]:
        """
        Given the HTML (ew) response from a successful upload, find the most
        recently uploaded torrent (it better be ours).
        """
        ids: list[tuple[int, int]] = []
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a", title="Permalink"):
            href = pl.get("href", "")
            match = re.search(r"torrents.php\?id=(\d+)\&torrentid=(\d+)", str(href))
            if match:
                ids.append((int(match[2]), int(match[1])))
        if not ids:
            raise TypeError("Could not parse torrent/group id from group page: no permalink ids found")
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
        url = self.base_url + "/reportsv2.php"
        # OPS only uses lossyapproval, not lossywebapproval
        data = {
            "auth": self.authkey,
            "torrentid": torrent_id,
            "categoryid": 1,
            "type": "lossyapproval",
            "extra": comment,
            "submit": True,
        }
        resp = await self._request("POST", url, params={"action": "takereport"}, data=data)
        if "torrents.php" in resp.url:
            return True
        raise RequestError(
            f"Failed to report torrent for lossy master: unexpected redirect to {resp.url} (status {resp.status})"
        )
