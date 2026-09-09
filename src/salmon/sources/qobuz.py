import re
from typing import Any

import msgspec

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper


class QobuzBase(BaseScraper):
    proxy_service = "qobuz"
    url = "https://www.qobuz.com/api.json/0.2"
    site_url = "https://www.qobuz.com"
    regex = re.compile(
        r"^https?://(?:www\.|play\.|open\.)?qobuz\.com/(?:(?:.+?/)?album/(?:.+?/)?|album/(?:-/)?)([a-zA-Z0-9]+)/?$"
    )
    release_format = "/album/get?album_id={rls_id}"
    get_params: dict[str, Any] | None = {}

    @staticmethod
    def configured() -> bool:
        """Qobuz refuses every API call without both an app id and a user token, so either missing means inactive."""
        return bool(cfg.metadata.qobuz.app_id and cfg.metadata.qobuz.user_auth_token)

    @property
    def headers(self) -> dict[str, str]:
        """Auth headers from the live config; unset values are left out rather than sent as None."""
        qobuz = cfg.metadata.qobuz
        candidates = {"X-App-Id": qobuz.app_id, "X-User-Auth-Token": qobuz.user_auth_token}
        return {key: value for key, value in candidates.items() if value}

    def require_configured(self) -> None:
        """Raise the ScrapeError the metadata step shows when a Qobuz URL is used without credentials."""
        if not self.configured():
            raise ScrapeError("Qobuz is inactive: set [metadata.qobuz] app_id and user_auth_token in config.toml")

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
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
        self.require_configured()
        try:
            match = self.regex.match(url)
            if not match:
                raise ScrapeError("Invalid Qobuz URL.")
            rls_id = match[1]
            return await self.get_json(self.release_format.format(rls_id=rls_id), params=params, headers=self.headers)
        except msgspec.DecodeError as e:
            raise ScrapeError("Qobuz page did not return valid JSON.") from e
        except (AttributeError, IndexError) as e:
            raise ScrapeError("Invalid Qobuz URL.") from e
