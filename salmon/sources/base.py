import asyncio
import json
import re
import traceback
from collections import namedtuple
from random import choice
from string import Formatter

import requests
from bs4 import BeautifulSoup

from salmon.constants import UAGENTS
from salmon.errors import ScrapeError

HEADERS = {"User-Agent": choice(UAGENTS)}

IdentData = namedtuple(
    "IdentData", ["artist", "album", "year", "track_count", "source"]
)

loop = asyncio.get_event_loop()


class BaseScraper:

    url = NotImplementedError
    site_url = NotImplementedError
    regex = NotImplementedError
    release_format = NotImplementedError
    get_params = {}

    @classmethod
    def format_url(cls, rls_id, rls_name=None):
        """
        Format the URL for a scraped release. The ``release_format``
        attribute of the scraper is processed and populated by the rls_id
        and rls_name. The rls_name is only relevant when back-filling
        into the sources that include release name in the URL. Those stores
        do not require the release name to reach the webpage, but re-adding
        something resembling the link doesn't harm us.
        """
        keys = [fn for _, fn, _, _ in Formatter().parse(cls.release_format) if fn]
        if "rls_name" in keys:
            rls_name = rls_name or "a"
            return cls.site_url + cls.release_format.format(
                rls_id=rls_id, rls_name=cls.url_format_rls_name(rls_name)
            )
        return cls.site_url + cls.release_format.format(rls_id=rls_id)

    async def get_json(self, url, params=None, headers=None):
        """
        Run an asynchronius GET request to a JSON API maintained by
        a metadata source.
        """
        return await loop.run_in_executor(
            None, lambda: self.get_json_sync(url, params, headers)
        )

    def get_json_sync(self, url, params=None, headers=None):
        """Make a synchronius get request, usually called by the async get_json."""
        params = {**(params or {}), **(self.get_params)}
        headers = {**(headers or {}), **HEADERS}
        try:
            result = requests.get(
                self.url + url, params=params, headers=headers, timeout=7
            )
            if result.status_code != 200:
                class_hierarchy = ' -> '.join([cls.__name__ for cls in self.__class__.mro()[:-1]])
                # traceback.print_stack()  # Removed to avoid confusion. ScrapeError already prints the stack trace.
                raise ScrapeError(f"{self.__class__.__name__}({class_hierarchy}): Status code {result.status_code}.", result.json())
            return result.json()
        except json.decoder.JSONDecodeError as e:
            raise ScrapeError(f"{self.__class__.__name__}: Did not receive JSON from API.") from e

    async def create_soup(self, url, params=None, headers=None, **kwargs):
        """
        Asynchroniously run a webpage scrape and return a BeautifulSoup
        object containing the scraped HTML.
        """
        params = params or {}
        r = await loop.run_in_executor(
            None,
            lambda: requests.get(
                url, params=params, headers=headers or HEADERS, timeout=7, **kwargs
            ),
        )
        if r.status_code != 200:
            raise ScrapeError(
                f"Failed to successfully scrape page. Status code: {r.status_code}"
            )
        return BeautifulSoup(r.text, "html.parser")

    @staticmethod
    def url_format_rls_name(rls_name):
        """
        Format the URL release name from the actual release name. This
        is not accurate to how the web stores do it; it is merely a
        convenience for user readability.
        """
        url = re.sub(r"[^\-a-z\d]", "", rls_name.lower().replace(" ", "-"))
        return re.sub("-+", "-", url)
