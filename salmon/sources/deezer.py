import asyncio
import json
import re
from json.decoder import JSONDecodeError
from random import choice

import requests

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
    url = "https://api.deezer.com"
    site_url = "https://www.deezer.com"
    regex = re.compile(r"^https*:\/\/.*?deezer\.com.*?\/(?:[a-z]+\/)?(album|playlist|track)\/([0-9]+)")
    release_format = "/album/{rls_id}"

    def __init__(self):
        self.country_code = None
        super().__init__()

        self._csrf_token = None
        self._login_csrf_token = None
        self._session = None

    @property
    def sesh(self):
        if self._session:
            return self._session

        self._session = requests.Session()
        return self._session

    @property
    def api_token(self):
        if self._csrf_token:
            return self._csrf_token

        params = {"api_version": "1.0", "api_token": "null", "input": "3"}
        response = self.sesh.get(
            "https://www.deezer.com/ajax/gw-light.php",
            params={"method": "deezer.getUserData", **params},
            headers=HEADERS,
        )
        try:
            check_data = json.loads(response.text)
            self._csrf_token = check_data["results"]["checkForm"]
            self._login_csrf_token = check_data["results"]["checkFormLogin"]
        except (JSONDecodeError, KeyError):
            pass
        return self._csrf_token

    @classmethod
    def parse_release_id(cls, url):
        return cls.regex.search(url)[2]

    async def create_soup(self, url, params=None):
        """Run a GET request to Deezer's JSON API for album data."""
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

    async def get_internal_api_data(self, url, params=None):
        """Deezer puts some things in an api that isn't public facing.
        Like track information and album art before a release is available.
        """
        loop = asyncio.get_running_loop()
        track_data = await loop.run_in_executor(
            None,
            lambda: self.sesh.get(self.site_url + url, params=(params or {})),
        )
        r = re.search(
            r"window.__DZR_APP_STATE__ = ({.*?}})</script>",
            track_data.text.replace("\n", ""),
        )
        if not r:
            raise ScrapeError("Failed to scrape track data.")
        raw = re.sub(r"{(\s*)type\: +\'([^\']+)\'", r'{\1type: "\2"', r[1])
        raw = re.sub("\t+([^:]+): ", r'"\1":', raw)
        return json.loads(raw)

    def get_tracks(self, internal_data):
        return internal_data["SONGS"]["data"]

    def get_cover(self, internal_data):
        "This uses a hardcoded url. Hope the dns url doesn't change."
        artwork_code = internal_data["DATA"]["ALB_PICTURE"]
        return f"https://e-cdns-images.dzcdn.net/images/cover/{artwork_code}/1000x1000-000000-100-0-0.jpg"
