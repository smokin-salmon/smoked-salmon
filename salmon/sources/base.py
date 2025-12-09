import json
import re
from collections import namedtuple
from random import choice
from string import Formatter

import aiohttp
from bs4 import BeautifulSoup

from salmon.constants import UAGENTS
from salmon.errors import ScrapeError

HEADERS = {"User-Agent": choice(UAGENTS)}

IdentData = namedtuple("IdentData", ["artist", "album", "year", "track_count", "source"])


class BaseScraper:
    """Base class for metadata scrapers."""

    url = NotImplementedError
    site_url = NotImplementedError
    regex = NotImplementedError
    release_format = NotImplementedError
    get_params = {}

    @classmethod
    def format_url(cls, rls_id: str, rls_name: str | None = None) -> str:
        """Format the URL for a scraped release.

        Args:
            rls_id: The release ID.
            rls_name: Optional release name for URL formatting.

        Returns:
            The formatted URL string.
        """
        keys = [fn for _, fn, _, _ in Formatter().parse(cls.release_format) if fn]
        if "rls_name" in keys:
            rls_name = rls_name or "a"
            return cls.site_url + cls.release_format.format(rls_id=rls_id, rls_name=cls.url_format_rls_name(rls_name))
        return cls.site_url + cls.release_format.format(rls_id=rls_id)

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        """Make async GET request to JSON API.

        Args:
            url: The URL path to request.
            params: Optional query parameters.
            headers: Optional HTTP headers.

        Returns:
            The JSON response as a dict.

        Raises:
            ScrapeError: If request fails or response is not JSON.
        """
        params = {**(params or {}), **(self.get_params)}
        headers = {**(headers or {}), **HEADERS}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(self.url + url, params=params, headers=headers) as resp,
            ):
                if resp.status != 200:
                    class_hierarchy = " -> ".join([cls.__name__ for cls in self.__class__.mro()[:-1]])
                    error_msg = f"{self.__class__.__name__}({class_hierarchy}): Status code {resp.status}."
                    try:
                        error_data = await resp.json()
                    except Exception:
                        error_data = None
                    raise ScrapeError(error_msg, error_data)
                return await resp.json()
        except aiohttp.ContentTypeError as e:
            raise ScrapeError(f"{self.__class__.__name__}: Did not receive JSON from API.") from e
        except json.decoder.JSONDecodeError as e:
            raise ScrapeError(f"{self.__class__.__name__}: Did not receive JSON from API.") from e

    async def create_soup(self, url: str, params: dict | None = None, headers: dict | None = None, **kwargs):
        """Scrape webpage and return BeautifulSoup object.

        Args:
            url: The URL to scrape.
            params: Optional query parameters.
            headers: Optional HTTP headers.
            **kwargs: Additional arguments for the request.

        Returns:
            BeautifulSoup object of the scraped page.

        Raises:
            ScrapeError: If scraping fails.
        """
        params = params or {}
        follow_redirects = kwargs.pop("follow_redirects", True)
        timeout = aiohttp.ClientTimeout(total=7)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                url,
                params=params,
                headers=headers or HEADERS,
                allow_redirects=follow_redirects,
                **kwargs,
            ) as r,
        ):
            if r.status != 200:
                raise ScrapeError(f"Failed to successfully scrape page. Status code: {r.status}")
            text = await r.text()
            return BeautifulSoup(text, "lxml")

    @staticmethod
    def url_format_rls_name(rls_name: str) -> str:
        """Format release name for URL.

        Args:
            rls_name: The release name.

        Returns:
            URL-formatted release name.
        """
        url = re.sub(r"[^\-a-z\d]", "", rls_name.lower().replace(" ", "-"))
        return re.sub("-+", "-", url)
