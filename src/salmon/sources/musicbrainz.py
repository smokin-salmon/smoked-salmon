import re
import threading
from collections.abc import Callable
from time import monotonic
from typing import Any
from urllib.error import HTTPError

import aiohttp
import anyio
import anyio.to_thread
import msgspec
import musicbrainzngs
from musicbrainzngs import musicbrainz as mb_module

from salmon import ssrf
from salmon.common.urls import parse_retry_after
from salmon.errors import ScrapeError
from salmon.release_notification import get_version

from .base import BaseScraper

musicbrainzngs.set_useragent("salmon", "1.0", "noreply@salm.on")

# musicbrainzngs has no timeout and tries a failing request up to 8 times (7 retries, sleeping 56 s in all). Every
# request it makes, the Cover Art Archive's too, goes through its private _safe_read, so that is where it is bounded.
# A test pins the parameters the wrapper passes.
MAX_REQUESTS = 2  # One retry, after its 2 s sleep
REQUEST_TIMEOUT = 10  # Seconds per socket operation

# MusicBrainz asks for a User-Agent naming the application and how to reach its makers.
USER_AGENT = f"smoked-salmon/{get_version() or 'unknown'} ( https://github.com/smokin-salmon/smoked-salmon/ )"
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
INCLUDES = (
    "artists",
    "labels",
    "recordings",
    "release-groups",
    "media",
    "artist-credits",
    "artist-rels",
    "recording-level-rels",
)
RETRY_WAIT = 2.0  # Seconds before the retry, unless a 503 names a wait of its own
MAX_RETRY_AFTER = 5.0  # A 503 asking for a longer wait is not retried

# MusicBrainz's rule: at most one request a second from each client.
MIN_INTERVAL = 1.0


class RateLimiter:
    """Spaces the calls to wait() `interval` seconds apart, across threads and event loops alike.

    Under salmon web each job has a thread and an event loop of its own, so the lock is a threading one, and each
    caller sleeps on its own loop, outside it.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        with self._lock:
            now = monotonic()
            start = max(now, self._next)
            self._next = start + self._interval
        if start > now:
            await anyio.sleep(start - now)


limiter = RateLimiter(MIN_INTERVAL)


class NotFoundError(ScrapeError):
    """MusicBrainz has no such release, or the Cover Art Archive no image for it (HTTP 404)."""

    def __init__(self) -> None:
        super().__init__("MusicBrainz: Not found.")


class _Transient(Exception):
    """A failed attempt worth one retry, `wait` seconds on; None: one not worth waiting for."""

    def __init__(self, error: ScrapeError, wait: float | None) -> None:
        super().__init__(str(error))
        self.error = error
        self.wait = wait


def _is_transient(cause: BaseException | None) -> bool:
    # An SSRF refusal is a connection error to aiohttp, but asking again is refused again.
    if isinstance(cause, ssrf.NonPublicAddressError):
        return False
    return isinstance(cause, (TimeoutError, aiohttp.ClientConnectionError))


# MusicBrainz stores these where a release has no catalogue number.
_PLACEHOLDER_CATNOS = frozenset({"none", "[none]", "(none)", "n/a", "[n/a]", "(n/a)"})


def is_placeholder_catno(catno: object) -> bool:
    """Whether a MusicBrainz catalogue number is a placeholder (``[none]``, ``n/a``...) and not a real number.

    Only an exact match counts: "non" or "None Audio" can be part of a real number.
    """
    return str(catno).strip().lower() in _PLACEHOLDER_CATNOS


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
    regex = re.compile(r"^https?://(?:www\.)?musicbrainz\.org/release/([a-f0-9\-]+)/?(?=$|[?#])")
    proxy_service = "musicbrainz"
    api_url = "https://musicbrainz.org/ws/2"
    caa_url = "https://coverartarchive.org"

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        """Ask MusicBrainz's API for JSON, a second after its last request.

        Args:
            url: The path under /ws/2, as "/release/<mbid>".
            params: Query parameters; fmt=json is added.
            headers: Extra headers; salmon's User-Agent and Accept replace any of the same name.

        Raises:
            NotFoundError: MusicBrainz has no such release.
            ScrapeError: If the request failed, again on its one retry where that may help, or did not answer JSON.
        """
        return await self._request_json(
            self.api_url + url, {**(params or {}), "fmt": "json"}, headers=headers, limited=True
        )

    async def fetch_cover_art(self, mbid: str) -> dict:
        """The Cover Art Archive's image list for a release. Another host than MusicBrainz's: not rate-limited.

        Raises:
            NotFoundError: The release has no image there.
            ScrapeError: If the request failed or did not answer JSON.
        """
        return await self._request_json(f"{self.caa_url}/release/{mbid}", {}, limited=False)

    async def _request_json(
        self, full_url: str, params: dict[str, str], *, headers: dict | None = None, limited: bool
    ) -> dict:
        sent = {**(headers or {}), **_HEADERS}
        for attempt in range(1, MAX_REQUESTS + 1):
            if limited:
                await limiter.wait()
            try:
                return await self._attempt(full_url, params, sent)
            except _Transient as e:
                if attempt == MAX_REQUESTS or e.wait is None:
                    raise e.error from e.error.__cause__
                await anyio.sleep(e.wait)
        raise AssertionError("unreachable")

    async def _attempt(self, full_url: str, params: dict[str, str], headers: dict[str, str]) -> dict:
        name = self.store
        try:
            async with self._session() as session, session.get(full_url, params=params, headers=headers) as resp:
                if resp.status == 404:
                    raise NotFoundError()
                if resp.status == 503:
                    retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                    wait = RETRY_WAIT if retry_after is None else retry_after
                    raise _Transient(
                        ScrapeError(f"{name}: Request failed (HTTP 503)."), wait if wait <= MAX_RETRY_AFTER else None
                    )
                if resp.status != 200:
                    raise ScrapeError(f"{name}: Request failed (HTTP {resp.status}).")
                body = await resp.read()
        except _Transient:
            raise
        except ScrapeError as e:
            if _is_transient(e.__cause__):
                raise _Transient(e, RETRY_WAIT) from e
            raise
        try:
            return msgspec.json.decode(body)
        except msgspec.DecodeError as e:
            raise ScrapeError(f"{name}: Did not receive JSON from API.") from e

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
            The release as MusicBrainz's JSON API gives it, with salmon's includes.

        Raises:
            ValueError: If URL is invalid.
            ScrapeError: If the request failed, or MusicBrainz has no such release.
        """
        match = self.regex.match(url)
        if not match:
            raise ValueError("Invalid MusicBrainz URL.")
        return await self.get_json(f"/release/{match[1]}", {"inc": " ".join(INCLUDES)})
