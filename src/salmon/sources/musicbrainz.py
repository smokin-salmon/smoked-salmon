import re
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError

import anyio.to_thread
import musicbrainzngs
from musicbrainzngs import musicbrainz as mb_module

from salmon.errors import ScrapeError

from .base import BaseScraper

musicbrainzngs.set_useragent("salmon", "1.0", "noreply@salm.on")

# musicbrainzngs has no timeout and tries a failing request up to 8 times (7 retries, sleeping 56 s in all). Every
# request it makes, the Cover Art Archive's too, goes through its private _safe_read, so that is where it is bounded.
# A test pins the parameters the wrapper passes.
MAX_REQUESTS = 2  # One retry, after its 2 s sleep
REQUEST_TIMEOUT = 10  # Seconds per socket operation

_safe_read = mb_module._safe_read


class _TimeoutOpener:
    """A urllib opener whose requests time out after REQUEST_TIMEOUT seconds."""

    def __init__(self, opener: Any) -> None:
        self._opener = opener

    def open(self, req: Any, body: Any = None) -> Any:
        return self._opener.open(req, body, timeout=REQUEST_TIMEOUT)


def _bounded_safe_read(opener, req, body=None):
    return _safe_read(_TimeoutOpener(opener), req, body, max_retries=MAX_REQUESTS)


mb_module._safe_read = _bounded_safe_read


class MusicBrainzBase(BaseScraper):
    store = "MusicBrainz"
    url = site_url = "https://musicbrainz.org"
    release_format = "/release/{rls_id}"
    regex = re.compile(r"^https?://(?:www\.)?musicbrainz.org/release/([a-f0-9\-]+)$")

    async def _call(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run a musicbrainzngs call in a thread, its errors as ScrapeError.

        The message names the error type, never the request URL, as get_json's do.

        Raises:
            ScrapeError: If the request failed, or MusicBrainz has no such release.
        """
        try:
            return await anyio.to_thread.run_sync(func, *args)
        except mb_module.WebServiceError as e:
            if isinstance(e.cause, HTTPError) and e.cause.code == 404:
                raise ScrapeError(f"{self.store or self.__class__.__name__}: Not found.") from e
            raise ScrapeError(f"{self.store or self.__class__.__name__}: Request failed ({type(e).__name__}).") from e

    async def fetch_data(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        rls_id: Any = None,
    ) -> dict[str, Any]:
        """Fetch release data from MusicBrainz API.

        Args:
            url: The MusicBrainz release URL.
            params: Unused, kept for API compatibility.
            headers: Unused, kept for API compatibility.
            follow_redirects: Unused, kept for API compatibility.

        Returns:
            Release data dict from MusicBrainz.

        Raises:
            ValueError: If URL is invalid.
            ScrapeError: If the request failed, or MusicBrainz has no such release.
        """
        match = re.search(r"/release/([a-f0-9\-]+)$", url)
        if not match:
            raise ValueError("Invalid MusicBrainz URL.")
        rls_id = match[1]
        return (
            await self._call(
                musicbrainzngs.get_release_by_id,
                rls_id,
                [
                    "artists",
                    "labels",
                    "recordings",
                    "release-groups",
                    "media",
                    "artist-credits",
                    "artist-rels",
                    "recording-level-rels",
                ],
            )
        )["release"]
