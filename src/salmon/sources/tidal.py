import re
from typing import Any

import msgspec

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper


class TidalBase(BaseScraper):
    proxy_service = "tidal"
    url = "https://api.tidalhifi.com/v1"
    site_url = "https://listen.tidal.com"
    image_url = "https://resources.tidal.com/images/{album_id}/1280x1280.jpg"
    regex = re.compile(r"^https*:\/\/.*?(?:tidal|wimpmusic)\.com.*?\/(album|track|playlist)\/([0-9a-z\-]+)")
    release_format = "/album/{rls_id}"
    get_params = {"token": cfg.metadata.tidal.token}

    @classmethod
    def format_url(cls, rls_id: Any, rls_name: str | None = None, url: str | None = None) -> str:
        if url:
            return url
        # rls_id is a tuple (country_code, release_id) for Tidal
        rls_id_str = rls_id[1] if isinstance(rls_id, tuple) else rls_id
        return cls.site_url + cls.release_format.format(rls_id=rls_id_str)

    @classmethod
    def parse_release_id(cls, url):
        match = cls.regex.search(url)
        if not match:
            raise ValueError("Invalid Tidal URL.")
        return match[2]

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
        """Fetch album data from Tidal's JSON API.

        Args:
            url: The Tidal album URL.
            params: Optional query parameters.
            headers: Unused, kept for API compatibility.
            follow_redirects: Unused, kept for API compatibility.
            rls_id: Release ID tuple ``(country_code, release_id)`` as produced
                by the multi-region search. Used to determine which storefront
                to query.

        Returns:
            Album data dict with tracklist.

        Raises:
            ScrapeError: If fetching fails.
        """
        params = params or {}
        album_id = self.parse_release_id(url)
        cc = rls_id[0] if isinstance(rls_id, tuple) else cfg.metadata.tidal.regions[0].upper()
        try:
            params["countrycode"] = cc
            data = await self.get_json(f"/albums/{album_id}", params=params)
            tracklist = await self.get_json(f"/albums/{album_id}/tracks", params=params)
            data["tracklist"] = tracklist["items"]
            data["_country_code"] = cc
            return data
        except msgspec.DecodeError as e:
            raise ScrapeError("Tidal page did not return valid JSON.") from e
        except (KeyError, ScrapeError) as e:
            raise ScrapeError(f"Failed to grab metadata for {url}.") from e
