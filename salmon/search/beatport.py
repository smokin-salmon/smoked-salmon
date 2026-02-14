import json
from typing import Any

from bs4 import BeautifulSoup

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search.base import IdentData, SearchMixin
from salmon.sources import BeatportBase
from salmon.sources.base import BaseScraper, SoupType


class Searcher(BeatportBase, SearchMixin):
    async def create_soup(
        self, url: str, params: dict | None = None, headers: dict | None = None, follow_redirects: bool = True
    ) -> SoupType:
        """Override to use BaseScraper's create_soup directly for search."""
        return await BaseScraper.create_soup(self, url, params, headers, follow_redirects)

    async def search_releases(self, searchstr: str, limit: int) -> tuple[str, dict]:
        releases: dict[Any, Any] = {}
        soup = await self.create_soup(self.search_url, params={"q": searchstr})
        if not isinstance(soup, BeautifulSoup):
            raise ScrapeError("Expected BeautifulSoup object")
        try:
            script_tag = soup.find("script", id="__NEXT_DATA__")
            if not script_tag:
                raise ScrapeError("Could not find Next.js data script tag")

            data = json.loads(script_tag.string or "")
            search_results = data["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["data"]
            for result in search_results:
                try:
                    rls_id = result["release_id"]

                    # Filter artists to get only main artists (not remixers)
                    main_artists = [a["artist_name"] for a in result["artists"] if a["artist_type_name"] == "Artist"]

                    title = result["release_name"]
                    artists = (
                        ", ".join(main_artists) if len(main_artists) < 4 else cfg.upload.formatting.various_artist_word
                    )
                    label = result["label"]["label_name"]

                    if label.lower() not in cfg.upload.search.excluded_labels:
                        releases[rls_id] = (
                            IdentData(artists, title, None, None, "WEB"),
                            self.format_result(artists, title, label),
                        )
                except (KeyError, IndexError) as e:
                    raise ScrapeError("Failed to parse search result item") from e

                if len(releases) == limit:
                    break

        except (KeyError, IndexError) as e:
            raise ScrapeError("Failed to parse scraped search results") from e

        return "Beatport", releases
