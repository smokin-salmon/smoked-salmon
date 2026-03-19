import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from salmon.errors import ScrapeError
from salmon.sources.base import HEADERS, BaseScraper

BANDCAMP_CHECKOUT_HOST = "bandcamp.com"
BANDCAMP_CHECKOUT_PATH = "/download"
BANDCAMP_RELEASE_PATH_RE = re.compile(r"^/(album|track)/[^/]+/?$", re.IGNORECASE)
BANDCAMP_DATABLOB_RE = re.compile(r'data-blob="([^"]+)"', re.IGNORECASE)


def normalize_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized


def is_checkout_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    host = normalize_host(parsed.netloc)
    if host != BANDCAMP_CHECKOUT_HOST:
        return False
    if parsed.path.rstrip("/") != BANDCAMP_CHECKOUT_PATH:
        return False
    return "cart_id" in parse_qs(parsed.query)


def normalize_release_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return None

    cleaned_path = parsed.path.rstrip("/")
    if not BANDCAMP_RELEASE_PATH_RE.match(f"{cleaned_path}/"):
        return None

    normalized = parsed._replace(path=cleaned_path, params="", query="", fragment="")
    return normalized.geturl()


def parse_checkout_data_blob(page_html: str) -> dict[str, Any]:
    match = BANDCAMP_DATABLOB_RE.search(page_html)
    if not match:
        raise ScrapeError("Bandcamp checkout page did not expose a data-blob payload")

    try:
        payload = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError as exc:
        raise ScrapeError("Bandcamp checkout page exposed an unreadable data-blob payload") from exc

    if not isinstance(payload, dict):
        raise ScrapeError("Bandcamp checkout page exposed an invalid data-blob payload")
    return payload


def iter_checkout_items(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    items: list[dict[str, Any]] = []
    for key in ("download_items", "digital_items"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            items.extend(item for item in candidate if isinstance(item, dict))

    if items:
        return items

    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if "page_url" in current and "downloads" in current:
                items.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return items


def resolve_release_url_from_checkout_html(page_html: str) -> str:
    payload = parse_checkout_data_blob(page_html)
    items = iter_checkout_items(payload)
    if not items:
        raise ScrapeError("Bandcamp checkout page did not expose any downloadable items")

    for item in items:
        raw_page_url = item.get("page_url")
        if isinstance(raw_page_url, str) and raw_page_url.strip():
            source_url = normalize_release_url(raw_page_url)
            if source_url:
                return source_url

    match = re.search(r"https://[^\"']+/(?:album|track)/[^\"'&<]+", page_html)
    if match:
        source_url = normalize_release_url(html.unescape(match.group(0)))
        if source_url:
            return source_url

    raise ScrapeError("Bandcamp checkout page did not expose a canonical album or track URL")


async def fetch_checkout_page_html(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=HEADERS) as response,
        ):
            if response.status != 200:
                raise ScrapeError(f"Failed to fetch Bandcamp checkout page. Status code: {response.status}")
            charset = response.charset or "utf-8"
            return (await response.read()).decode(charset, errors="replace")
    except (TimeoutError, aiohttp.ClientError) as exc:
        raise ScrapeError(f"Failed to fetch Bandcamp checkout page: {exc}") from exc


async def resolve_source_url(url: str) -> str:
    normalized = normalize_release_url(url)
    if normalized:
        return normalized
    if not is_checkout_url(url):
        return url.strip()

    page_html = await fetch_checkout_page_html(url)
    return resolve_release_url_from_checkout_html(page_html)


class BandcampBase(BaseScraper):
    is_json_api = False
    search_url = "https://bandcamp.com/search/"
    regex = re.compile(r"^https?://([^/]+)/(album|track)/([^/]+)/?")
    release_format = "https://{rls_url}/{type}/{rls_id}"

    @classmethod
    def format_url(cls, rls_id: Any, rls_name: str | None = None, url: str | None = None) -> str:
        if url:
            return url
        # rls_id is expected to be a tuple of (domain, type, id)
        return cls.release_format.format(rls_url=rls_id[0], type=rls_id[1], rls_id=rls_id[2])
