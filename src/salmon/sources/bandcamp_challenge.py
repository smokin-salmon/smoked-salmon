"""Bandcamp's "Client Challenge" page (#985).

From some networks Bandcamp answers a page with HTTP 200 and a JavaScript check titled "Client Challenge" instead of
the release. A page of Bandcamp's own hosts is then asked for once more as a browser (curl_cffi impersonating Chrome),
as yt-dlp does; if that still fails, or the page is on an artist's own domain, a ScrapeError says so.
"""

from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from salmon.errors import ScrapeError
from salmon.proxy import curl_url

STILL_CHALLENGED = (
    'Bandcamp sent a check page ("Client Challenge") instead of the release, also when asked again as a browser. '
    "Use another source, or try again later."
)
CHALLENGED = (
    'Bandcamp sent a check page ("Client Challenge") instead of the release. Use another source, or try again later.'
)


def is_challenge(soup: BeautifulSoup) -> bool:
    """Whether `soup` is Bandcamp's check page rather than the page asked for."""
    return soup.title is not None and soup.title.get_text().strip().lower() == "client challenge"


async def retry_as_browser(url: str, params: dict | None, timeout: float) -> BeautifulSoup:
    """Ask once more for `url`, which got the check page, as a browser would.

    Only a page of Bandcamp's own hosts is asked for again: the request skips salmon web's public-address guard
    (salmon.ssrf), which a host Bandcamp does not own needs. A redirect is not followed.

    Raises:
        ScrapeError: If the page is not asked for again, the request fails, or it answers with anything but the page.
    """
    host = (urlsplit(url).hostname or "").lower()
    if host != "bandcamp.com" and not host.endswith(".bandcamp.com"):
        raise ScrapeError(CHALLENGED)
    status, data = await _impersonated_get(url, params, curl_url("bandcamp"), timeout)
    if status != 200:
        raise ScrapeError(STILL_CHALLENGED)
    soup = BeautifulSoup(data, "lxml")
    if is_challenge(soup):
        raise ScrapeError(STILL_CHALLENGED)
    return soup


async def _impersonated_get(url: str, params: dict | None, proxy: str | None, timeout: float) -> tuple[int, bytes]:
    """GET `url` impersonating Chrome, without following a redirect; returns the status and body.

    Without `proxy`, it connects directly: libcurl would otherwise take a proxy from the environment (http_proxy,
    ALL_PROXY), which salmon's other requests ignore.

    Raises:
        ScrapeError: If curl_cffi does not load, or the request fails. It names the error's type, never the URL.
    """
    try:
        # Here, so that salmon starts without it.
        from curl_cffi import AsyncSession, CurlError
    except (ImportError, OSError) as e:
        raise ScrapeError(f"Bandcamp: Could not load curl_cffi to ask again as a browser ({type(e).__name__}).") from e
    try:
        async with AsyncSession() as session:
            response = await session.get(
                url,
                params=params,
                # An empty proxy is libcurl's "connect directly"; curl_cffi passes one on only in `proxies`.
                proxies={"all": proxy or ""},
                timeout=timeout,
                allow_redirects=False,
                impersonate="chrome",
            )
    except (CurlError, TimeoutError) as e:
        raise ScrapeError(f"Bandcamp: Request failed when asked again as a browser ({type(e).__name__}).") from e
    return response.status_code, response.content
