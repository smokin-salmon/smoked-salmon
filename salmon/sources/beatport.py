import json
import re

from salmon.errors import ScrapeError
from salmon.sources.base import BaseScraper


class BeatportBase(BaseScraper):
    url = site_url = "https://beatport.com"
    search_url = "https://beatport.com/search/releases"
    release_format = "/release/{rls_name}/{rls_id}"
    regex = re.compile(r"^https?://(?:(?:www|classic)\.)?beatport\.com/release/.+?/(\d+)/?$")

    async def create_soup(self, url, params=None, headers=None, **kwargs):  # type: ignore[override]
        """Extract JSON data from Beatport's HTML page."""
        soup = await super().create_soup(url, params)
        try:
            script_tag = soup.find("script", id="__NEXT_DATA__")  # type: ignore[union-attr]
            if not script_tag:
                raise ScrapeError("Could not find Next.js data script tag")

            data = json.loads(script_tag.string or "")
            queries = data["props"]["pageProps"]["dehydratedState"]["queries"]

            track_query = next((q for q in queries if q.get("queryKey") and q["queryKey"][0] == "tracks"), None)

            if not track_query:
                raise ScrapeError("Could not find track data in page")

            # print(json.dumps(track_query))
            return track_query

        except json.JSONDecodeError as e:
            raise ScrapeError("Failed to parse Beatport JSON data") from e
        except (KeyError, AttributeError) as e:
            raise ScrapeError(f"Failed to extract required data from Beatport page: {str(e)}") from e
