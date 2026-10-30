import re
from random import choice
from typing import Any

import aiohttp
import asyncclick as click
import msgspec

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


async def album_upc(url: str) -> str | None:
    """The UPC of the Deezer album at `url`; None for any other URL or when Deezer cannot be read."""
    match = DeezerBase.regex.search(url)
    if not match or match[1] != "album":
        return None
    try:
        data = await DeezerBase().get_json(f"/album/{match[2]}", headers=HEADERS)
    except (ScrapeError, aiohttp.ClientError, msgspec.DecodeError, TimeoutError) as error:
        # The barcode is an optional extra, so a Deezer outage must not stop the upload, but say
        # so: otherwise a blank catalogue number looks like "this release has no barcode".
        click.secho(f"Could not read the barcode from Deezer ({type(error).__name__}); leaving it blank.", fg="yellow")
        return None
    if isinstance(data, dict) and isinstance(error := data.get("error"), dict):
        # Deezer answers a quota or a lookup it refuses with HTTP 200 and this, not with an error status.
        reason = str(error.get("message") or error.get("type") or "an error")[:100]
        click.secho(f"Could not read the barcode from Deezer ({reason}); leaving it blank.", fg="yellow")
        return None
    upc = data.get("upc") if isinstance(data, dict) else None
    return str(upc) if upc else None


class DeezerBase(BaseScraper):
    store = "Deezer"
    """Base scraper for Deezer metadata."""

    proxy_service = "deezer"
    url = "https://api.deezer.com"
    site_url = "https://www.deezer.com"
    regex = re.compile(
        r"^https?:\/\/(?:[a-z0-9-]+\.)*deezer\.com\/(?:[a-z]+\/)?(album|playlist|track)\/([0-9]+)(?=$|[/?#])"
    )
    release_format = "/album/{rls_id}"

    def __init__(self) -> None:
        """Initialize Deezer scraper."""
        self.country_code = None
        super().__init__()

    @classmethod
    def parse_release_id(cls, url: str) -> str:
        """Parse release ID from Deezer URL.

        Args:
            url: The Deezer URL.

        Returns:
            The release ID.

        Raises:
            ValueError: If URL doesn't match expected pattern.
        """
        match = cls.regex.search(url)
        if not match:
            raise ValueError(f"Invalid Deezer URL: {url}")
        return match[2]

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
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
        except msgspec.DecodeError as e:
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
        async with (
            self._session() as session,
            session.get(self.site_url + url, params=(params or {}), headers=HEADERS) as response,
        ):
            if response.status != 200:
                raise ScrapeError(f"Deezer internal API returned status {response.status}.")
            text = await response.text()

        r = re.search(
            r"window.__DZR_APP_STATE__ = ({.*?}})</script>",
            text.replace("\n", ""),
        )
        if not r:
            raise ScrapeError("Failed to scrape track data.")
        raw = re.sub(r"{(\s*)type\: +\'([^\']+)\'", r'{\1type: "\2"', r[1])
        raw = re.sub("\t+([^:]+): ", r'"\1":', raw)
        return msgspec.json.decode(raw)

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
