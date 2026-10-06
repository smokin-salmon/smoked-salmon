from typing import Any

import asyncclick as click

from salmon import cfg
from salmon.common import UploadFiles
from salmon.errors import UploadRefusedError
from salmon.trackers.base import BaseGazelleApi, TagRules

# The options of the sample rate field on DIC's upload form, which the form has and requires
# for 24bit Lossless only. An explicit table, so a rate the form does not list has no value.
SAMPLE_RATES = {
    44100: "44.1kHz",
    48000: "48kHz",
    88200: "88.2kHz",
    96000: "96kHz",
    176400: "176.4kHz",
    192000: "192kHz",
}


def _khz(rate: int) -> str:
    return f"{rate / 1000:g} kHz"


class DICApi(BaseGazelleApi):
    # DIC's upload form has no Arranger role (unconfirmed whether it ever will).
    unsupported_artist_roles = frozenset({"arranger"})
    # Path limit unconfirmed for DIC; kept at the same value as before this was per-tracker.
    TAG_RULES = TagRules(max_path_length=180)

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

    def upload_form_fields(self, metadata: dict[str, Any], track_data: dict[str, Any]) -> dict[str, str]:
        """Give the sample rate DIC requires for a 24bit Lossless torrent.

        Args:
            metadata: Release metadata of the torrent being uploaded.
            track_data: Track information of the files in that torrent.

        Returns:
            The sample_rate field for a 24bit Lossless torrent, nothing for any other.

        Raises:
            UploadRefusedError: If the files have mixed sample rates, or one the form does not list.
        """
        if metadata["encoding"] != "24bit Lossless":
            return {}

        rates = sorted({track["sample rate"] for track in track_data.values()})
        if len(rates) != 1:
            found = ", ".join(_khz(rate) for rate in rates) or "none"
            raise UploadRefusedError(
                f"{self.site_string} takes one sample rate per torrent, and the files of this one have "
                f"{found}: not uploading it there."
            )
        rate = rates[0]
        if rate not in SAMPLE_RATES:
            offered = ", ".join(_khz(rate) for rate in SAMPLE_RATES)
            raise UploadRefusedError(
                f"The files of this torrent are {_khz(rate)}, and {self.site_string}'s upload form has no "
                f"sample rate option for it (only {offered}): not uploading it there."
            )
        return {"sample_rate": SAMPLE_RATES[rate]}

    def skip_upload_marks(self) -> None:
        """Send none of the Self-purchased, Self-rip and Exclusive marks, and do not ask for them."""
        self.specific_params = {}
        self._marks_prompted = True

    async def upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Attempt to upload a torrent to the site using the upload.php.

        Args:
            data: Upload form data dictionary.
            files: UploadFiles containing files to upload.

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

        return await super().upload(enriched_data, files)
