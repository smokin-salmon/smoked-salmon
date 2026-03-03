import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from salmon.errors import ScrapeError

from .base import BaseScraper

AMP_API_URL = "https://amp-api.music.apple.com"
APPLE_MUSIC_URL = "https://music.apple.com"


class iTunesBase(BaseScraper):
    url = AMP_API_URL
    site_url = APPLE_MUSIC_URL
    search_url = "https://itunes.apple.com/search"
    regex = re.compile(r"^https?://(itunes|music)\.apple\.com/(?:(\w{2,4})/)?album/(?:[^/]*/)?([^\?]+)")
    release_format = "/album/-/{rls_id}"
    get_params: dict[str, Any] | None = None

    _token: str | None = None

    @classmethod
    def format_url(cls, rls_id: Any, rls_name: str | None = None, url: str | None = None) -> str:
        """Format an Apple Music album URL from a release ID.

        ``rls_id`` may be either a plain collection ID (int/str) or a
        ``(storefront, collection_id)`` tuple as produced by the multi-region
        search.
        """
        if url:
            return url
        if isinstance(rls_id, tuple):
            # key is (storefront, lang, collection_id) from multi-region search
            storefront, _, collection_id = rls_id
            return f"{cls.site_url}/{storefront}/album/-/{collection_id}"
        return f"{cls.site_url}/album/-/{rls_id}"

    @classmethod
    async def _get_token(cls) -> str:
        """Fetch Bearer token from Apple Music web app JS bundle.

        Returns:
            Bearer token string.

        Raises:
            ScrapeError: If token cannot be extracted.
        """
        if cls._token:
            return cls._token

        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(APPLE_MUSIC_URL, allow_redirects=True) as resp:
                    if resp.status != 200:
                        raise ScrapeError("Failed to load Apple Music homepage")
                    html = await resp.text()

                matches = re.findall(r"/assets/index~[^/\"]+\.js", html)
                if not matches:
                    raise ScrapeError("Could not find index JS bundle URL on Apple Music homepage")
                js_uri = matches[0]

                async with session.get(APPLE_MUSIC_URL + js_uri) as js_resp:
                    if js_resp.status != 200:
                        raise ScrapeError("Failed to load Apple Music JS bundle")
                    js_text = await js_resp.text()

                token_match = re.search(r'eyJh([^"]*)', js_text)
                if not token_match:
                    raise ScrapeError("Could not extract Bearer token from Apple Music JS bundle")

                cls._token = token_match.group(0)
                return cls._token

        except aiohttp.ClientError as e:
            raise ScrapeError("Network error while fetching Apple Music token") from e

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
        """Fetch release data from Apple Music amp-api.

        Args:
            url: The Apple Music album URL.
            params: Unused, kept for interface compatibility.
            headers: Unused, kept for interface compatibility.
            follow_redirects: Unused, kept for interface compatibility.
            rls_id: Release ID tuple ``(storefront, lang, collection_id)`` as
                produced by the multi-region search. Used to determine which
                storefront and language to query.

        Returns:
            A dict with keys ``album`` (album attributes dict) and
            ``tracks`` (list of track attribute dicts).

        Raises:
            ScrapeError: If URL parsing or API request fails.
        """
        match = self.regex.match(url)
        if not match:
            raise ScrapeError("Invalid Apple Music URL format")

        album_id = match.group(3).split("?")[0].split("/")[0]

        if isinstance(rls_id, tuple):
            sf, lang, _ = rls_id
        else:
            sf = match.group(2) or "us"
            qs = parse_qs(urlparse(url).query)
            lang = qs.get("l", ["en-US"])[0]

        token = await self._get_token()
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Origin": APPLE_MUSIC_URL,
        }

        try:
            data = await self.get_json(
                f"{AMP_API_URL}/v1/catalog/{sf}/albums/{album_id}",
                params={"include": "tracks", "l": lang},
                headers=request_headers,
            )

            album_data = data["data"][0]
            tracks_rel = album_data.get("relationships", {}).get("tracks", {})
            tracks_data = list(tracks_rel.get("data", []))
            tracks_next = tracks_rel.get("next")

            offset = len(tracks_data)
            while tracks_next:
                page_data = await self.get_json(
                    f"{AMP_API_URL}/v1/catalog/{sf}/albums/{album_id}/tracks",
                    params={"offset": offset, "l": lang},
                    headers=request_headers,
                )
                tracks_data.extend(page_data.get("data", []))
                tracks_next = page_data.get("next")
                offset += len(page_data.get("data", []))

            return {
                "album": album_data.get("attributes", {}),
                "tracks": tracks_data,
            }

        except (KeyError, IndexError) as e:
            raise ScrapeError(f"Failed to grab Apple Music metadata for {url}.") from e
