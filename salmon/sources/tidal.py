import json
import re
from typing import Any

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper, SoupType

# Unused parameter in override - kept for API compatibility
# ruff: noqa: ARG002


class TidalBase(BaseScraper):
    url = "https://api.tidalhifi.com/v1"
    site_url = "https://listen.tidal.com"
    image_url = "https://resources.tidal.com/images/{album_id}/1280x1280.jpg"
    regex = re.compile(r"^https*:\/\/.*?(?:tidal|wimpmusic)\.com.*?\/(album|track|playlist)\/([0-9a-z\-]+)")
    release_format = "/album/{rls_id}"
    get_params = {"token": cfg.metadata.tidal.token}

    def __init__(self):
        self.country_code = None
        super().__init__()

    @classmethod
    def format_url(cls, rls_id: Any, rls_name: str | None = None, url: str | None = None) -> str:
        if url:
            return url
        # rls_id is a tuple (type, id) for Tidal
        rls_id_str = rls_id[1] if isinstance(rls_id, tuple) else rls_id
        return cls.site_url + cls.release_format.format(rls_id=rls_id_str)

    @classmethod
    def parse_release_id(cls, url):
        match = cls.regex.search(url)
        if not match:
            raise ValueError("Invalid Tidal URL.")
        return match[2]

    async def create_soup(
        self, url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True
    ) -> SoupType:
        """Run a GET request to Tidal's JSON API for album data.

        Args:
            url: The Tidal album URL.
            params: Optional query parameters.
            headers: Unused, kept for API compatibility.
            follow_redirects: Accepted for API compatibility but not used
                by the underlying JSON API calls.

        Returns:
            Album data dict with tracklist.

        Raises:
            ScrapeError: If fetching fails for all regions.
        """
        params = params or {}
        album_id = self.parse_release_id(url)
        for cc in get_tidal_regions_to_fetch():
            try:
                self.country_code = cc
                params["countrycode"] = cc
                data = await self.get_json(f"/albums/{album_id}", params=params)
                tracklist = await self.get_json(f"/albums/{album_id}/tracks", params=params)
                data["tracklist"] = tracklist["items"]
                return data
            except json.decoder.JSONDecodeError as e:
                raise ScrapeError("Tidal page did not return valid JSON.") from e
            except (KeyError, ScrapeError):
                pass
        raise ScrapeError(f"Failed to grab metadata for {url}.")


def get_tidal_regions_to_fetch():
    # TODO: maybe make this a validation
    if cfg.metadata.tidal.fetch_regions:
        return cfg.metadata.tidal.fetch_regions
    else:
        raise ScrapeError("No regions defined for Tidal to grab from")
