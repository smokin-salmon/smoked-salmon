import re
from email.utils import parsedate_to_datetime
from functools import cache
from time import monotonic, time
from typing import Any, ClassVar

import aiohttp
import anyio
import asyncclick as click
import msgspec

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.proxy import session_kwargs
from salmon.sources.base import BaseScraper

# Tidal V2 mediaTags, best quality first.
QUALITY_MAP = {
    "HIRES_LOSSLESS": "HI_RES",
    "LOSSLESS": "LOSSLESS",
    "DOLBY_ATMOS": "DOLBY_ATMOS",
    "MP3_320": "MP3",
}

# Cap on the cursor pages followed for one listing, so a malformed nextCursor can't loop forever.
MAX_PAGES = 50
# A request Tidal rate limits (HTTP 429) is retried at most this many times, and only when
# the wait it asks for is at most MAX_RETRY_WAIT seconds.
RATE_LIMIT_RETRIES = 2
MAX_RETRY_WAIT = 30.0

# The value the old config.default.toml shipped for the retired token.
_TOKEN_PLACEHOLDER = "your-token"


def parse_quality(media_tags: list[str]) -> str | None:
    """Map Tidal V2 mediaTags to the best audio quality they list."""
    for tag, quality in QUALITY_MAP.items():
        if tag in media_tags:
            return quality
    return None


def credentials_configured() -> bool:
    """Check whether Tidal client credentials are set.

    A config written for Tidal's retired API only has a ``token``, which no longer works;
    the user is told once how to switch to client credentials.
    """
    tidal = cfg.metadata.tidal
    if tidal.client_id and tidal.client_secret:
        return True
    if tidal.token and tidal.token != _TOKEN_PLACEHOLDER:
        _notify_retired_token()
    return False


@cache
def _notify_retired_token() -> None:
    click.secho(
        "Tidal: [metadata.tidal] token only worked with Tidal's retired API, so Tidal is skipped. "
        "Register a client at https://developer.tidal.com and set client_id and client_secret instead.",
        fg="yellow",
    )


def _parse_retry_after(value: str | None) -> float | None:
    """Get the wait in seconds from a Retry-After header (delay-seconds or HTTP-date)."""
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        return max(parsedate_to_datetime(value).timestamp() - time(), 0.0)
    except (TypeError, ValueError):
        return None


class _RateLimitedError(ScrapeError):
    def __init__(self, retry_after: float | None):
        self.retry_after = retry_after
        super().__init__("Tidal rate limit exceeded (HTTP 429).")


class TidalBase(BaseScraper):
    proxy_service = "tidal"
    url = "https://openapi.tidal.com/v2"
    token_url = "https://auth.tidal.com/v1/oauth2/token"
    site_url = "https://listen.tidal.com"
    regex = re.compile(r"^https*:\/\/.*?(?:tidal|wimpmusic)\.com.*?\/(album|track|playlist)\/([0-9a-z\-]+)")
    release_format = "/album/{rls_id}"

    # Shared by every Tidal scraper and searcher, so one token serves them all.
    _access_token: ClassVar[str | None] = None
    _token_expiry: ClassVar[float] = 0.0
    _token_lock: ClassVar[anyio.Lock] = anyio.Lock()

    @classmethod
    async def _ensure_token(cls) -> str:
        """Return a valid OAuth2 access token, fetching one if needed."""
        if TidalBase._access_token and monotonic() < TidalBase._token_expiry - 60:
            return TidalBase._access_token
        if not credentials_configured():
            raise ScrapeError("Tidal client_id and client_secret are not set.")
        # Serialize refreshes so a cold cache doesn't fire N concurrent auth POSTs.
        async with cls._token_lock:
            if TidalBase._access_token and monotonic() < TidalBase._token_expiry - 60:
                return TidalBase._access_token
            timeout = aiohttp.ClientTimeout(total=10)
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout, **session_kwargs(cls.proxy_service)) as session,
                    session.post(
                        cls.token_url,
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
            except (TimeoutError, aiohttp.ClientError) as e:
                raise ScrapeError(f"Tidal authentication failed: {e}") from e
            try:
                data = msgspec.json.decode(body)
                token = data["access_token"]
                TidalBase._token_expiry = monotonic() + data["expires_in"]
            except (msgspec.DecodeError, KeyError, TypeError) as e:
                raise ScrapeError(f"Tidal authentication returned an unexpected response: {e}") from e
            TidalBase._access_token = token
            return token

    async def handle_json_response(self, resp: aiohttp.ClientResponse) -> dict:
        if resp.status == 429:
            raise _RateLimitedError(_parse_retry_after(resp.headers.get("Retry-After")))
        return await super().handle_json_response(resp)

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        """Make an authenticated request to the Tidal API.

        A rate-limited request is retried after the wait Tidal asks for (or a short
        backoff when it names none), at most RATE_LIMIT_RETRIES times.
        """
        retries = 0
        while True:
            token = await self._ensure_token()
            auth_headers = {
                **(headers or {}),
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.api+json",
            }
            try:
                return await super().get_json(url, params=params, headers=auth_headers)
            except _RateLimitedError as e:
                wait = e.retry_after if e.retry_after is not None else 2.0**retries
                if retries >= RATE_LIMIT_RETRIES or wait > MAX_RETRY_WAIT:
                    raise
                retries += 1
                await anyio.sleep(wait)

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

    @staticmethod
    def next_cursor(links: dict) -> str | None:
        """Get the cursor of the next page from a JSON:API links object."""
        return links.get("meta", {}).get("nextCursor")

    @staticmethod
    def _parse_resource_artists(resource: dict, included: list[dict]) -> list[dict]:
        """Get the artists of a resource, in order, from the included artist resources."""
        artist_ids = [rel["id"] for rel in resource.get("relationships", {}).get("artists", {}).get("data", [])]
        by_id = {obj["id"]: obj for obj in included if obj["type"] == "artists"}
        return [
            {"id": artist_id, "name": by_id[artist_id]["attributes"]["name"]}
            for artist_id in artist_ids
            if artist_id in by_id
        ]

    def _build_soup(self, album: dict[str, Any], included: list[dict], items: list[dict], cc: str) -> dict[str, Any]:
        """Build a normalized soup dict from an album resource and its included resources."""
        attributes = album["attributes"]
        cover = None
        for obj in included:
            if obj["type"] == "artworks" and obj["attributes"]["mediaType"] == "IMAGE":
                files = obj["attributes"].get("files", [])
                if files:
                    cover = max(files, key=lambda f: f.get("meta", {}).get("width", 0))["href"]
                break

        tracks = {obj["id"]: obj for obj in included if obj["type"] == "tracks"}
        tracklist = []
        for item in items:
            track = tracks.get(item["id"])
            if not track:
                continue
            meta = item.get("meta", {})
            track_attrs = track["attributes"]
            tracklist.append(
                {
                    "id": item["id"],
                    "volumeNumber": meta.get("volumeNumber"),
                    "trackNumber": meta.get("trackNumber"),
                    "title": track_attrs.get("title"),
                    "version": track_attrs.get("version"),
                    "isrc": track_attrs.get("isrc"),
                    "explicit": track_attrs.get("explicit"),
                    "audioQuality": parse_quality(track_attrs.get("mediaTags", [])),
                    "artists": self._parse_resource_artists(track, included),
                }
            )

        return {
            "id": album["id"],
            "title": attributes.get("title"),
            "type": attributes.get("albumType"),
            "releaseDate": attributes.get("releaseDate"),
            "copyright": (attributes.get("copyright") or {}).get("text"),
            "upc": attributes.get("barcodeId"),
            "cover": cover,
            "numberOfTracks": attributes.get("numberOfItems"),
            "explicit": attributes.get("explicit"),
            "artists": self._parse_resource_artists(album, included),
            "tracklist": tracklist,
            "_country_code": cc,
        }

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
            params: Unused, kept for API compatibility.
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
            doc = await self.get_json(
                f"/albums/{album_id}",
                params={"countryCode": cc, "include": "artists,items,items.artists,coverArt"},
            )
            album = doc["data"]
            included = doc.get("included", [])
            # JSON:API omits `links` on a single page and may omit an empty relationship.
            items_rel = album.get("relationships", {}).get("items", {})
            items = items_rel.get("data", [])
            cursor = self.next_cursor(items_rel.get("links", {}))
            for _ in range(MAX_PAGES):
                if not cursor:
                    break
                page = await self.get_json(
                    f"/albums/{album_id}/relationships/items",
                    params={"countryCode": cc, "include": "items,items.artists", "page[cursor]": cursor},
                )
                items += page["data"]
                included += page.get("included", [])
                cursor = self.next_cursor(page.get("links", {}))
            if cursor:
                raise ScrapeError(f"Tidal album {album_id} has more than {MAX_PAGES} pages of tracks.")
            return self._build_soup(album, included, items, cc)
        except msgspec.DecodeError as e:
            raise ScrapeError("Tidal page did not return valid JSON.") from e
        except (KeyError, ScrapeError) as e:
            raise ScrapeError(f"Failed to grab metadata for {url}.") from e
