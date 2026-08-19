import re
from time import monotonic
from typing import Any, ClassVar

import aiohttp
import anyio
import msgspec

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper

QUALITY_MAP = {
    "HIRES_LOSSLESS": "HI_RES",
    "LOSSLESS": "LOSSLESS",
    "DOLBY_ATMOS": "DOLBY_ATMOS",
    "MP3_320": "MP3",
}


def parse_quality(media_tags: list[str]) -> str | None:
    """Map Tidal V2 mediaTags to a single audio quality value."""
    for tag in media_tags:
        if tag in QUALITY_MAP:
            return QUALITY_MAP[tag]
    return None


class TidalBase(BaseScraper):
    url = "https://openapi.tidal.com/v2"
    site_url = "https://listen.tidal.com"
    regex = re.compile(r"^https*:\/\/.*?(?:tidal|wimpmusic)\.com.*?\/(album|track|playlist)\/([0-9a-z\-]+)")
    release_format = "/album/{rls_id}"
    get_params = None

    _access_token: str | None = None
    _token_expiry: float = 0.0
    _token_lock: ClassVar[anyio.Lock] = anyio.Lock()

    @classmethod
    async def _ensure_token(cls) -> str:
        """Return a valid OAuth2 access token, fetching one if needed."""
        if cls._access_token and monotonic() < cls._token_expiry - 60:
            return cls._access_token
        # Serialize refreshes so a cold cache doesn't fire N concurrent auth POSTs.
        async with cls._token_lock:
            if cls._access_token and monotonic() < cls._token_expiry - 60:
                return cls._access_token
            timeout = aiohttp.ClientTimeout(total=10)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    "https://auth.tidal.com/v1/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": cfg.metadata.tidal.client_id,
                        "client_secret": cfg.metadata.tidal.client_secret,
                    },
                ) as resp,
            ):
                body = await resp.read()
                if resp.status != 200:
                    raise ScrapeError(f"Tidal authentication failed (HTTP {resp.status}).")
            try:
                data = msgspec.json.decode(body)
                token = data["access_token"]
                cls._access_token = token
                cls._token_expiry = monotonic() + data["expires_in"]
            except (msgspec.DecodeError, KeyError, TypeError) as e:
                raise ScrapeError(f"Tidal authentication returned an unexpected response: {e}") from e
            return token

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        token = await self._ensure_token()
        headers = {
            **(headers or {}),
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.api+json",
        }
        return await super().get_json(url, params=params, headers=headers)

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

    async def _get_album_resources(self, album_id: str, cc: str) -> dict[str, Any]:
        """Fetch an album document with its items, artists and cover art."""
        return await self.get_json(
            f"/albums/{album_id}",
            params={
                "countryCode": cc,
                "include": "artists,items,items.artists,coverArt",
            },
        )

    async def _get_items_page(self, album_id: str, cc: str, cursor: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"countryCode": cc, "include": "items"}
        if cursor:
            params["page[cursor]"] = cursor
        return await self.get_json(f"/albums/{album_id}/relationships/items", params=params)

    def _build_soup(
        self,
        doc: dict[str, Any],
        album_id: str,
        cc: str,
        album_artists: list[dict],
        items: list[dict],
        tracks: dict[str, dict],
    ) -> dict[str, Any]:
        """Build a normalized soup dict from an album resource document."""
        album = doc["data"]
        attributes = album["attributes"]
        cover = None
        for obj in doc.get("included", []):
            if obj["type"] == "artworks" and obj["attributes"]["mediaType"] == "IMAGE":
                files = obj["attributes"].get("files", [])
                if files:
                    cover = max(files, key=lambda f: f["meta"].get("width", 0))["href"]
                break

        tracklist = []
        for item in items:
            meta = item.get("meta", {})
            track_id = item["id"]
            track = tracks.get(track_id)
            if not track:
                continue
            track_attrs = track["attributes"]
            tracklist.append(
                {
                    "volumeNumber": meta.get("volumeNumber"),
                    "trackNumber": meta.get("trackNumber"),
                    "title": track_attrs.get("title"),
                    "version": track_attrs.get("version"),
                    "replayGain": None,
                    "peak": None,
                    "isrc": track_attrs.get("isrc"),
                    "explicit": track_attrs.get("explicit"),
                    "audioQuality": parse_quality(track_attrs.get("mediaTags", [])),
                    "allowStreaming": None,
                    "id": track_id,
                    "artists": self._parse_resource_artists(track, doc.get("included", [])),
                }
            )

        return {
            "id": album_id,
            "title": attributes.get("title"),
            "type": attributes.get("albumType") or attributes.get("type"),
            "releaseDate": attributes.get("releaseDate"),
            "copyright": (attributes.get("copyright") or {}).get("text"),
            "upc": attributes.get("barcodeId"),
            "cover": cover,
            "numberOfTracks": attributes.get("numberOfItems"),
            "explicit": attributes.get("explicit"),
            "artists": album_artists,
            "tracklist": tracklist,
            "_country_code": cc,
        }

    @staticmethod
    def _parse_resource_artists(resource: dict, included: list[dict]) -> list[dict]:
        artist_ids = [rel["id"] for rel in resource.get("relationships", {}).get("artists", {}).get("data", [])]
        by_id = {obj["id"]: obj for obj in included if obj["type"] == "artists"}
        return [
            {"id": artist_id, "name": by_id[artist_id]["attributes"]["name"]}
            for artist_id in artist_ids
            if artist_id in by_id
        ]

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
        """Fetch album data from Tidal's V2 JSON API.

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
        album_id = self.parse_release_id(url)
        cc = rls_id[0] if isinstance(rls_id, tuple) else cfg.metadata.tidal.regions[0].upper()
        try:
            doc = await self._get_album_resources(album_id, cc)
            items = doc["data"]["relationships"]["items"]["data"]
            tracks = {obj["id"]: obj for obj in doc.get("included", []) if obj["type"] == "tracks"}
            album_artists = self._parse_resource_artists(doc["data"], doc.get("included", []))

            cursor = doc["data"]["relationships"]["items"]["links"].get("meta", {}).get("nextCursor")
            pages = 0
            while cursor and pages < 50:  # cap paging so a malformed nextCursor can't loop forever
                pages += 1
                page = await self._get_items_page(album_id, cc, cursor)
                items += page["data"]
                for obj in page.get("included", []):
                    if obj["type"] == "tracks":
                        tracks.setdefault(obj["id"], obj)
                cursor = page.get("links", {}).get("meta", {}).get("nextCursor")

            return self._build_soup(doc, album_id, cc, album_artists, items, tracks)
        except msgspec.DecodeError as e:
            raise ScrapeError("Tidal page did not return valid JSON.") from e
        except (KeyError, ScrapeError) as e:
            raise ScrapeError(f"Failed to grab metadata for {url}.") from e
