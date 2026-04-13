import re
from random import choice
from string import Formatter
from typing import Any

import aiohttp
import msgspec
from bs4 import BeautifulSoup

from salmon.constants import UAGENTS
from salmon.errors import ScrapeError

HEADERS = {"User-Agent": choice(UAGENTS)}


class IdentData(msgspec.Struct, frozen=True):
    """Data structure for release identification."""

    artist: str
    album: str
    year: int | str | None
    track_count: int | None
    source: str


class BaseScraper:
    """Base class for metadata scrapers."""

    url: str = ""
    site_url: str = ""
    regex: re.Pattern[str]
    release_format: str = ""
    get_params: dict[str, Any] | None = None
    is_json_api: bool = True

    @classmethod
    def format_url(cls, rls_id: Any, rls_name: str | None = None, url: str | None = None) -> str:
        """Format the URL for a scraped release.

        Args:
            rls_id: The release ID.
            rls_name: Optional release name for URL formatting.
            url: Optional pre-formatted URL to return directly.

        Returns:
            The formatted URL string.
        """
        if url:
            return url
        keys = [fn for _, fn, _, _ in Formatter().parse(cls.release_format) if fn]
        if "rls_name" in keys:
            rls_name = rls_name or "a"
            return cls.site_url + cls.release_format.format(rls_id=rls_id, rls_name=cls.url_format_rls_name(rls_name))
        # Handle tuple rls_id (e.g., from Tidal)
        rls_id_str = rls_id[1] if isinstance(rls_id, tuple) else rls_id
        return cls.site_url + cls.release_format.format(rls_id=rls_id_str)

    async def handle_json_response(self, resp: aiohttp.ClientResponse) -> dict:
        if resp.status != 200:
            class_hierarchy = " -> ".join([cls.__name__ for cls in self.__class__.mro()[:-1]])
            error_msg = f"{self.__class__.__name__}({class_hierarchy}): Status code {resp.status}."
            try:
                error_data = await resp.text()
            except Exception:
                error_data = None
            raise ScrapeError(error_msg, error_data)
        return msgspec.json.decode(await resp.read())

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        """Make async GET request to JSON API.

        Args:
            url: Full URL or a path that will be appended to ``self.url``.
                 If the value starts with ``http://`` or ``https://`` it is
                 used as-is; otherwise ``self.url`` is prepended.
            params: Optional query parameters.
            headers: Optional HTTP headers.

        Returns:
            The JSON response as a dict.

        Raises:
            ScrapeError: If request fails or response is not JSON.
        """
        params = {**(params or {}), **(self.get_params or {})}
        params = {k: v for k, v in params.items() if v is not None}  # remove None values before serializing
        headers = {**(headers or {}), **HEADERS}
        full_url = url if url.startswith(("http://", "https://")) else self.url + url
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(full_url, params=params, headers=headers) as resp,
            ):
                return await self.handle_json_response(resp)
        except aiohttp.ContentTypeError as e:
            raise ScrapeError(f"{self.__class__.__name__}: Did not receive JSON from API.") from e
        except msgspec.DecodeError as e:
            raise ScrapeError(f"{self.__class__.__name__}: Did not receive JSON from API.") from e

    async def fetch_page(
        self, url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True
    ) -> BeautifulSoup:
        """Scrape webpage and return BeautifulSoup object.

        Args:
            url: The URL to scrape.
            params: Optional query parameters.
            headers: Optional HTTP headers.
            follow_redirects: Whether to follow redirects. Defaults to True.

        Returns:
            BeautifulSoup object of the scraped page.

        Raises:
            ScrapeError: If scraping fails.
        """
        params = params or {}
        timeout = aiohttp.ClientTimeout(total=7)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(
                    url,
                    params=params,
                    headers=headers or HEADERS,
                    allow_redirects=follow_redirects,
                ) as r,
            ):
                if r.status != 200:
                    raise ScrapeError(f"Failed to successfully scrape page. Status code: {r.status}")
                data = await r.read()
                return BeautifulSoup(data, "lxml")
        except (TimeoutError, aiohttp.ClientError) as e:
            raise ScrapeError(f"Failed to scrape page: {e}") from e

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
        """Fetch release data from a source.

        Subclasses that use JSON APIs should override this method to return
        parsed release data.  The default implementation raises
        ``NotImplementedError``.

        Args:
            :param url: The release URL.
            :param params: Optional query parameters.
            :param headers: Optional HTTP headers.
            :param follow_redirects: Whether to follow redirects.
            :param rls_id: The Release ID.

        Returns:
            Release data dict.

        Raises:
            NotImplementedError: Always, unless overridden.
        """
        raise NotImplementedError

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
