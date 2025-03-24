import re

from salmon import config
from salmon.errors import ScrapeError
from salmon.search.base import IdentData, SearchMixin
from salmon.sources import BeatportBase


class Searcher(BeatportBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        releases = {}
        soup = await self.create_soup(self.search_url, params={"q": searchstr}, follow_redirects=True)
        for meta in soup.select(".bucket-items.ec-bucket li .release-meta"):
            try:
                rls_id = int(
                    re.search(r"/release/.+?/(\d+)$", meta.find("a")["href"])[1]
                )
                ar_li = [
                    a.string for a in meta.select(".release-artists a") if a.string
                ]
                title = next(
                    t.string for t in meta.select(".release-title a") if t.string
                )
                artists = (
                    ", ".join(ar_li) if len(ar_li) < 4 else config.VARIOUS_ARTIST_WORD
                )
                label = meta.select(".release-label a")[0].string
                if label.lower() not in config.SEARCH_EXCLUDED_LABELS:
                    releases[rls_id] = (
                        IdentData(artists, title, None, None, "WEB"),
                        self.format_result(artists, title, label),
                    )
            except (TypeError, IndexError) as e:
                raise ScrapeError("Failed to parse scraped search results.") from e
            if len(releases) == limit:
                break
        return "Beatport", releases
