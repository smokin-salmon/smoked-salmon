import json
import re
from typing import Any

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper, SoupType


class QobuzBase(BaseScraper):
    url = "https://www.qobuz.com/api.json/0.2"
    site_url = "https://www.qobuz.com"
    regex = re.compile(
        r"^https?://(?:www\.|play\.)?qobuz\.com/(?:(?:.+?/)?album/(?:.+?/)?|album/(?:-/)?)([a-zA-Z0-9]+)/?$"
    )
    release_format = "/album/get?album_id={rls_id}"
    headers = {
        "X-App-Id": cfg.metadata.qobuz.app_id,
        "X-User-Auth-Token": cfg.metadata.qobuz.user_auth_token,
    }
    get_params: dict[str, Any] | None = {}

    async def create_soup(
        self, url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True
    ) -> SoupType:
        """Fetch album data from Qobuz JSON API.

        Args:
            url: The Qobuz album URL.
            params: Optional query parameters.
            headers: Unused, kept for API compatibility.
            follow_redirects: Unused, kept for API compatibility.

        Returns:
            Album data dict from Qobuz API.

        Raises:
            ScrapeError: If URL is invalid or request fails.
        """
        try:
            match = self.regex.match(url)
            if not match:
                raise ScrapeError("Invalid Qobuz URL.")
            rls_id = match[1]
            return await self.get_json(self.release_format.format(rls_id=rls_id), params=params, headers=self.headers)
        except json.decoder.JSONDecodeError as e:
            raise ScrapeError("Qobuz page did not return valid JSON.") from e
        except (AttributeError, IndexError) as e:
            raise ScrapeError("Invalid Qobuz URL.") from e
