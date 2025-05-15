from salmon.trackers.base import BaseGazelleApi

from salmon import config
import click
import requests
from requests.exceptions import ConnectTimeout, ReadTimeout

import asyncio
import re
from bs4 import BeautifulSoup

from salmon.common import flush_stdin
from salmon.errors import (
    LoginError,
    RequestError,
    RequestFailedError,
)

loop = asyncio.get_event_loop()

class DICApi(BaseGazelleApi):
    def __init__(self):
        self.headers = {
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "User-Agent": config.USER_AGENT,
        }
        self.site_code = 'DIC'
        self.base_url = 'https://dicmusic.com'
        self.tracker_url = 'https://tracker.52dic.vip'
        self.site_string = 'DICMusic'
        self.dot_torrents_dir = config.DOTTORRENTS_DIR

        self.cookie = config.DIC_SESSION

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        self.authkey = None
        self.passkey = None
        self.authenticate()

    async def site_page_upload(self, data, files):
        """Attempt to upload a torrent to the site.
        using the upload.php"""
        url = self.base_url + "/upload.php"
        data["auth"] = self.authkey

        if data["media"] == "WEB":
            source = click.prompt(
                click.style("\n"
                    "You are uploading a WEB release to DIC.\n"
                    "According to the site rules, you need to enter the source here,\n"
                    "otherwise the torrent will be marked as trumpable.",
                    fg="cyan",
                    bold=True,
                ),
                default="",
            )
            
            if source:
                data['release_desc'] += f"[b]Source:[/b] [url][plain]{source}[/plain][/url]\n"

        flush_stdin()
        mark = click.prompt(
            click.style("\n"
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
            flush_stdin()
            mark = click.prompt(
                click.style("\n"
                    "Do you want to mark this torrent as 'Exclusive'?\n"
                    "[E]xclusive, [N]one",
                    fg="magenta",
                    bold=True,
                ),
                type=click.STRING,
                default="N",
            )[0].lower()
            if mark == "e":
                data["jinzhuan"] = "on"

        resp = await loop.run_in_executor(
            None,
            lambda: self.session.post(
                url, data=data, files=files, headers=self.headers
            ),
        )

        if self.announce in resp.text:
            match = re.search(
                r'<p style="color: red; text-align: center;">(.+)<\/p>', resp.text,
            )
            if match:
                raise RequestError(
                    f"Site upload failed: {match[1]} ({resp.status_code})"
                )
        if 'requests.php' in resp.url:
            try:
                torrent_id = self.parse_torrent_id_from_filled_request_page(resp.text)
                click.secho(f"Filled request: {resp.url}", fg="green")
                return torrent_id
            except (TypeError, ValueError):
                soup = BeautifulSoup(resp.text, "html.parser")
                error = soup.find('h2', text='Error')
                if error:
                    error_message = error.parent.parent.find('p').text
                raise RequestError(f"Request fill failed: {error_message}")
        try:
            return self.parse_most_recent_torrent_and_group_id_from_group_page(
                resp.text
            )
        except TypeError:
            raise RequestError(f"Site upload failed, response text: {resp.text}")