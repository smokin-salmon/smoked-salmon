import json
import re

from bs4 import BeautifulSoup

from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper, SoupType


class BeatportBase(BaseScraper):
    url = site_url = "https://beatport.com"
    search_url = "https://beatport.com/search/releases"
    release_format = "/release/{rls_name}/{rls_id}"
    regex = re.compile(r"^https?://(?:(?:www|classic)\.)?beatport\.com/release/.+?/(\d+)/?$")

    async def create_soup(
        self, url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True
    ) -> SoupType:
        """Extract JSON track data from Beatport's HTML page.

        Args:
            url: The Beatport URL.
            params: Optional query parameters.
            headers: Optional HTTP headers.
            follow_redirects: Whether to follow redirects.

        Returns:
            Track query dict extracted from page data.

        Raises:
            ScrapeError: If extraction fails.
        """
        soup = await super().create_soup(url, params)
        if not isinstance(soup, BeautifulSoup):
            raise ScrapeError("Expected BeautifulSoup object from parent create_soup")
        try:
            script_tag = soup.find("script", id="__NEXT_DATA__")
            if not script_tag or not script_tag.string:
                raise ScrapeError("Could not find Next.js data script tag")

            data = json.loads(script_tag.string)
            queries = data["props"]["pageProps"]["dehydratedState"]["queries"]

            track_query = next((q for q in queries if q.get("queryKey") and q["queryKey"][0] == "tracks"), None)

            if not track_query:
                raise ScrapeError("Could not find track data in page")

            return track_query

        except json.JSONDecodeError as e:
            raise ScrapeError("Failed to parse Beatport JSON data") from e
        except (KeyError, AttributeError) as e:
            raise ScrapeError(f"Failed to extract required data from Beatport page: {e}") from e
