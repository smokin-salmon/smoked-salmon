import json
import re
from json.decoder import JSONDecodeError
from random import choice

import aiohttp

from salmon.constants import UAGENTS
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper

HEADERS = {
    "User-Agent": choice(UAGENTS),
    "Content-Language": "en-US",
    "Cache-Control": "max-age=0",
    "Accept": "*/*",
    "Accept-Charset": "utf-8,ISO-8859-1;q=0.7,*;q=0.3",
    "Accept-Language": "en",
}


class DeezerBase(BaseScraper):
    """Base scraper for Deezer metadata."""

    url = "https://api.deezer.com"
    site_url = "https://www.deezer.com"
    regex = re.compile(r"^https*:\/\/.*?deezer\.com.*?\/(?:[a-z]+\/)?(album|playlist|track)\/([0-9]+)")
    release_format = "/album/{rls_id}"

    def __init__(self) -> None:
        """Initialize Deezer scraper."""
        self.country_code = None
        super().__init__()
        self._csrf_token: str | None = None
        self._login_csrf_token: str | None = None

    async def _ensure_api_token(self) -> str | None:
        """Fetch API token from Deezer if not cached.

        Returns:
            The CSRF token or None if failed.
        """
        if self._csrf_token:
            return self._csrf_token

        params = {"api_version": "1.0", "api_token": "null", "input": "3", "method": "deezer.getUserData"}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(
                    "https://www.deezer.com/ajax/gw-light.php",
                    params=params,
                    headers=HEADERS,
                ) as response,
            ):
                check_data = await response.json()
                self._csrf_token = check_data["results"]["checkForm"]
                self._login_csrf_token = check_data["results"]["checkFormLogin"]
        except (JSONDecodeError, KeyError, aiohttp.ClientError):
            pass
        return self._csrf_token

    @classmethod
    def parse_release_id(cls, url: str) -> str:
        """Parse release ID from Deezer URL.

        Args:
            url: The Deezer URL.

        Returns:
            The release ID.
        """
        return cls.regex.search(url)[2]

    async def create_soup(self, url: str, params: dict | None = None):
        """Fetch album data from Deezer API.

        Args:
            url: The Deezer album URL.
            params: Optional query parameters.

        Returns:
            Album data dict with tracklist and cover.

        Raises:
            ScrapeError: If fetching fails.
        """
        params = params or {}
        album_id = self.parse_release_id(url)
        try:
            data = await self.get_json(f"/album/{album_id}", params=params, headers=HEADERS)
            internal_data = await self.get_internal_api_data(f"/album/{album_id}", params)
            data["tracklist"] = self.get_tracks(internal_data)
            data["cover_xl"] = self.get_cover(internal_data)
            return data
        except json.decoder.JSONDecodeError as e:
            raise ScrapeError("Deezer page did not return valid JSON.") from e
        except (KeyError, ScrapeError) as e:
            raise ScrapeError(f"Failed to grab metadata for {url}.") from e

    async def get_internal_api_data(self, url: str, params: dict | None = None) -> dict:
        """Fetch internal API data from Deezer.

        Deezer puts some things in an API that isn't public facing,
        like track information and album art before a release is available.

        Args:
            url: The URL path.
            params: Optional query parameters.

        Returns:
            Internal API data dict.

        Raises:
            ScrapeError: If scraping fails.
        """
        timeout = aiohttp.ClientTimeout(total=10)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(self.site_url + url, params=(params or {}), headers=HEADERS) as response,
        ):
            text = await response.text()

        r = re.search(
            r"window.__DZR_APP_STATE__ = ({.*?}})</script>",
            text.replace("\n", ""),
        )
        if not r:
            raise ScrapeError("Failed to scrape track data.")
        raw = re.sub(r"{(\s*)type\: +\'([^\']+)\'", r'{\1type: "\2"', r[1])
        raw = re.sub("\t+([^:]+): ", r'"\1":', raw)
        return json.loads(raw)

    def get_tracks(self, internal_data: dict) -> list:
        """Extract track list from internal data.

        Args:
            internal_data: The internal API data.

        Returns:
            List of track data.
        """
        return internal_data["SONGS"]["data"]

    def get_cover(self, internal_data: dict) -> str:
        """Extract cover URL from internal data.

        Args:
            internal_data: The internal API data.

        Returns:
            The cover image URL.
        """
        artwork_code = internal_data["DATA"]["ALB_PICTURE"]
        return f"https://e-cdns-images.dzcdn.net/images/cover/{artwork_code}/1000x1000-000000-100-0-0.jpg"
