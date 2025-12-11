import json
import re

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper, SoupType


class DiscogsBase(BaseScraper):
    url = "https://api.discogs.com"
    site_url = "https://www.discogs.com"
    regex = re.compile(r"^https?://(?:www\.)?discogs\.com/(?:.+?/)?release/(\d+)/?$")
    release_format = "/release/{rls_id}"
    get_params = {"token": cfg.metadata.discogs_token}

    async def create_soup(
        self, url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True
    ) -> SoupType:
        match = self.regex.match(url)
        if not match:
            raise ScrapeError("Invalid Discogs URL.")
        try:
            return await self.get_json(f"/releases/{match[1]}", params=params)
        except json.decoder.JSONDecodeError as e:
            raise ScrapeError("Discogs page did not return valid JSON.") from e
