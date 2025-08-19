import click

from salmon import cfg
from salmon.trackers.base import BaseGazelleApi


class DICApi(BaseGazelleApi):
    def __init__(self):
        self.site_code = "DIC"
        self.base_url = "https://dicmusic.com"
        self.tracker_url = "https://tracker.52dic.vip"
        self.site_string = "DICMusic"
        if cfg.tracker.dic:
            dic_cfg = cfg.tracker.dic
            if dic_cfg.dottorrents_dir:
                self.dot_torrents_dir = dic_cfg.dottorrents_dir
            else:
                self.dot_torrents_dir = cfg.directory.dottorrents_dir

            self.cookie = dic_cfg.session
            if dic_cfg.api_key:
                self.api_key = dic_cfg.api_key

        super().__init__()

    async def site_page_upload(self, data, files):
        """Attempt to upload a torrent to the site.
        using the upload.php"""

        mark = click.prompt(
            click.style(
                "\n"
                "Do you want to mark this torrent as 'Self-purchased' or 'Self-rip'?\n"
                "Please note that selecting these marks for a re-posted torrent may result in a warning.\n"
                "Self-[p]urchased, Self-[r]ip, [N]one",
                fg="magenta",
                bold=True,
            ),
            type=click.STRING,
            default="N",
        )[0].lower()
        if mark == "p":
            data["buy"] = "on"
        elif mark == "r":
            data["diy"] = "on"

        if mark == "p" or mark == "r":
            mark = click.prompt(
                click.style(
                    "\nDo you want to mark this torrent as 'Exclusive'?\n[E]xclusive, [N]one",
                    fg="magenta",
                    bold=True,
                ),
                type=click.STRING,
                default="N",
            )[0].lower()
            if mark == "e":
                data["jinzhuan"] = "on"

        return await super().site_page_upload(data, files)
