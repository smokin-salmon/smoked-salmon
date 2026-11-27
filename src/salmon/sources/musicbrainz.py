import re
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, nullcontext
from time import monotonic
from typing import Any
from urllib.parse import urljoin

import aiohttp
import anyio
import msgspec

from salmon import ssrf
from salmon.common.urls import parse_retry_after
from salmon.errors import ScrapeError
from salmon.release_notification import get_version

from .base import BaseScraper

MAX_REQUESTS = 2  # One retry, and only for a failure that may pass
MAX_REDIRECTS = 5  # The Cover Art Archive sends its image list on to archive.org in two
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

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
_BUSY_POLL = 0.05  # Seconds between looks at the limiter while another request has the turn


class RateLimiter:
    """One request at a time, each sent at least `interval` seconds after the one before it ended.

    A turn lasts from before connecting until the answer is read, so a slow connection cannot send its request
    beside the next one's. Under salmon web each job has a thread and an event loop of its own, so the lock is a
    threading one, held only to read and set the state, and each caller sleeps on its own loop, outside it. The clock
    is checked again after every sleep: a caller that wakes late takes the next free turn.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._busy = False
        self._ended = float("-inf")

    @asynccontextmanager
    async def turn(self) -> AsyncGenerator[None]:
        while True:
            with self._lock:
                now = monotonic()
                ready = self._ended + self._interval
                if not self._busy and now >= ready:
                    self._busy = True
                    break
                wait = _BUSY_POLL if self._busy else ready - now
            await anyio.sleep(wait)
        try:
            yield
        finally:
            with self._lock:
                self._busy = False
                self._ended = monotonic()


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
    # An SSRF refusal and a TLS failure are connection errors to aiohttp, but asking again is refused again.
    if isinstance(cause, (ssrf.NonPublicAddressError, aiohttp.ClientSSLError)):
        return False
    return isinstance(cause, (TimeoutError, aiohttp.ClientConnectionError))


# MusicBrainz stores these where a release has no catalogue number.
_PLACEHOLDER_CATNOS = frozenset({"none", "[none]", "(none)", "n/a", "[n/a]", "(n/a)"})


def is_placeholder_catno(catno: object) -> bool:
    """Whether a MusicBrainz catalogue number is a placeholder (``[none]``, ``n/a``...) and not a real number.

    Only an exact match counts: "non" or "None Audio" can be part of a real number.
    """
    return str(catno).strip().lower() in _PLACEHOLDER_CATNOS


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
            try:
                return await self._attempt(full_url, params, sent, limited=limited)
            except _Transient as e:
                if attempt == MAX_REQUESTS or e.wait is None:
                    raise e.error from e.error.__cause__
                await anyio.sleep(e.wait)
        raise AssertionError("unreachable")

    async def _attempt(self, url: str, params: dict[str, str], headers: dict[str, str], *, limited: bool) -> dict:
        try:
            async with self._session() as session:
                # The retry in _request_json is the only one: aiohttp's own resend of a GET whose connection
                # dropped would double the requests, and go out past the limiter.
                session._retry_connection = False
                body = await self._follow(session, url, params, headers, limited=limited)
        except _Transient:
            raise
        except ScrapeError as e:
            if _is_transient(e.__cause__):
                raise _Transient(e, RETRY_WAIT) from e
            raise
        try:
            data = msgspec.json.decode(body)
        except msgspec.DecodeError as e:
            raise ScrapeError(f"{self.store}: Did not receive JSON from API.") from e
        if not isinstance(data, dict):
            raise ScrapeError(f"{self.store}: Did not receive JSON from API.")
        return data

    async def _follow(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: dict[str, str] | None,
        headers: dict[str, str],
        *,
        limited: bool,
    ) -> bytes:
        """The body at the end of `url`'s redirects, each hop sent on a turn of the limiter of its own.

        aiohttp would follow up to ten redirects in one turn: a merged release's old MBID is a 301.
        """
        for _ in range(MAX_REDIRECTS + 1):
            async with (
                limiter.turn() if limited else nullcontext(),
                session.get(url, params=params, headers=headers, allow_redirects=False) as resp,
            ):
                location = resp.headers.get("Location")
                if resp.status in _REDIRECT_STATUSES and location:
                    url, params = urljoin(str(resp.url), location), None
                    continue
                self._check_status(resp)
                return await resp.read()
        raise ScrapeError(f"{self.store}: Request failed (too many redirects).")

    def _check_status(self, resp: aiohttp.ClientResponse) -> None:
        self.record_answer(resp.status)
        if resp.status == 404:
            raise NotFoundError()
        if resp.status == 503:
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            wait = RETRY_WAIT if retry_after is None else retry_after
            raise _Transient(
                ScrapeError(f"{self.store}: Request failed (HTTP 503)."), wait if wait <= MAX_RETRY_AFTER else None
            )
        if resp.status != 200:
            raise ScrapeError(f"{self.store}: Request failed (HTTP {resp.status}).")

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
