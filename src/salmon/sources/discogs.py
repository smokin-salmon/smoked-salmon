import re
from typing import Any

import msgspec

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper


class DiscogsBase(BaseScraper):
    url = "https://api.discogs.com"
    site_url = "https://www.discogs.com"
    regex = re.compile(r"^https?://(?:www\.)?discogs\.com/(?:.+?/)?release/(\d+)")
    release_format = "/release/{rls_id}"
    get_params = {"token": cfg.metadata.discogs_token}

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
        """Fetch release data from Discogs JSON API.

        Args:
            url: The Discogs release URL.
            params: Optional query parameters.
            headers: Optional HTTP headers (forwarded to get_json).
            follow_redirects: Unused, kept for API compatibility.

        Returns:
            Release data dict from Discogs API.

        Raises:
            ScrapeError: If URL is invalid or request fails.
        """
        match = self.regex.match(url)
        if not match:
            raise ScrapeError("Invalid Discogs URL.")
        try:
            return await self.get_json(f"/releases/{match[1]}", params=params)
        except msgspec.DecodeError as e:
            raise ScrapeError("Discogs page did not return valid JSON.") from e
