import json
import re

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper


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
    get_params = {}

    async def create_soup(self, url, params=None, headers=None, **kwargs):  # type: ignore[override]
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
