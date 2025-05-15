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


class SMApi(BaseGazelleApi):
    def __init__(self):
        self.headers = {
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "User-Agent": config.USER_AGENT,
        }
        self.site_code = 'SM'
        self.base_url = 'https://sugoimusic.me'
        self.tracker_url = 'https://migoto.sugoimusic.me'
        self.site_string = 'SugoiMusic'
        self.dot_torrents_dir = config.DOTTORRENTS_DIR

        self.cookie = config.SM_SESSION
        self.userid = config.SM_USERID

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        self.authkey = None
        self.passkey = None
        self.authenticate()
        

    def authenticate(self):
        """Make a request to the site API with the saved cookie and get our authkey."""
        self.session.cookies.clear()
        self.session.cookies["session"] = self.cookie
        self.session.cookies["userid"] = self.userid
        try:
            acctinfo = loop.run_until_complete(self.request("index"))
        except RequestError:
            raise LoginError
        self.authkey = acctinfo["authkey"]
        self.passkey = acctinfo["passkey"]
