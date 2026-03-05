import asyncclick as click
from aiohttp import FormData

from salmon import cfg
from salmon.trackers.base import BaseGazelleApi


class DICApi(BaseGazelleApi):
    def __init__(self):
        self.site_code = "DIC"
        self.base_url = "https://dicmusic.com"
        self.tracker_url = "https://tracker.52dic.vip"
        self.site_string = "DICMusic"

        self._marks_prompted = False
        self.specific_params = {}

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

    async def site_page_upload(self, data: dict, files: FormData) -> tuple[int, int]:
        """Attempt to upload a torrent to the site using the upload.php.

        Args:
            data: Upload form data dictionary.
            files: FormData containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id) from the upload response.
        """
        if not self._marks_prompted:
            # Prompt for mark type
            raw_mark = await click.prompt(
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
            )
            mark = raw_mark[0].lower() if raw_mark else "n"

            # Build mark parameters immutably
            mark_params = {}
            if mark == "p":
                mark_params["buy"] = "on"
            elif mark == "r":
                mark_params["diy"] = "on"

            # Prompt for exclusive mark if needed
            if mark in ("p", "r"):
                raw_excl = await click.prompt(
                    click.style(
                        "\nDo you want to mark this torrent as 'Exclusive'?\n[E]xclusive, [N]one",
                        fg="magenta",
                        bold=True,
                    ),
                    type=click.STRING,
                    default="N",
                )
                excl = raw_excl[0].lower() if raw_excl else "n"
                if excl == "e":
                    mark_params["jinzhuan"] = "on"

            # Update params and mark as prompted
            self.specific_params = mark_params
            self._marks_prompted = True

        # Merge data with params (no filtering needed)
        enriched_data = {**data, **self.specific_params}

        return await super().site_page_upload(enriched_data, files)
