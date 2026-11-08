import re
from typing import Any

from bs4 import BeautifulSoup

from salmon.sources import bandcamp_challenge
from salmon.sources.base import PAGE_TIMEOUT, BaseScraper


class BandcampBase(BaseScraper):
    store = "Bandcamp"
    proxy_service = "bandcamp"
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

    async def fetch_page(
        self, url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True
    ) -> BeautifulSoup:
        """BaseScraper.fetch_page, asking once more as a browser for a page that got Bandcamp's check page (#985).

        Raises:
            ScrapeError: If scraping fails, or the check page stays (see bandcamp_challenge.retry_as_browser).
        """
        soup, final = await self._fetch_page_at(url, params, headers, follow_redirects)
        if not bandcamp_challenge.is_challenge(soup):
            return soup
        # The retry asks for the page the check page came from, after any redirect. That URL carries the query asked
        # for, unless a redirect dropped it: only the params it lacks are passed again, so none is doubled.
        missing = {key: value for key, value in (params or {}).items() if key not in final.query}
        return await bandcamp_challenge.retry_as_browser(str(final), missing or None, PAGE_TIMEOUT)
