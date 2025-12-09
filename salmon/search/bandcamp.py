import re

from salmon.errors import ScrapeError
from salmon.search.base import IdentData, SearchMixin
from salmon.sources import BandcampBase


class Searcher(BandcampBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        releases = {}
        try:
            soup = await self.create_soup(self.search_url, params={"q": searchstr}, follow_redirects=False)
            for meta in soup.select(".result-items .searchresult.data-search .result-info"):  # type: ignore[union-attr]
                try:
                    item_url_elem = meta.select(".itemurl a")[0]
                    item_url_str = item_url_elem.string if item_url_elem else None
                    if not item_url_str:
                        continue
                    re_url = self.regex.search(item_url_str)
                    if not re_url:
                        continue

                    rls_url = re.sub(r"\?.+", "", re_url[1])
                    release_type = re_url[2]  # 'album' or 'track'
                    rls_id = re_url[3]

                    heading_elem = meta.select(".heading a")[0]
                    title_str = heading_elem.string if heading_elem else None
                    title = title_str.strip() if title_str else ""
                    if len(title) > 100:
                        title = f"{title[:98]}.."

                    subhead_match = re.search("by (.+)", meta.select(".subhead")[0].text)
                    artists = subhead_match[1].strip() if subhead_match else ""

                    # For single tracks, there's just one track
                    length_match = re.search(r"(\d+) tracks?", meta.select(".length")[0].text)
                    track_count = 1 if release_type == "track" else int(length_match[1]) if length_match else 1

                    releaser = rls_url.split(".bandcamp.com")[0]
                    date = meta.select(".released")[0].text.strip()
                    year_match = re.search(r"(\d{4})", date)
                    year = year_match[1] if year_match else None

                    releases[(rls_url, release_type, rls_id)] = (
                        IdentData(artists, title, year, track_count, "WEB"),
                        self.format_result(artists, title, f"{year} {releaser}", track_count=track_count),
                    )

                    if len(releases) == limit:
                        break

                except (TypeError, IndexError):
                    continue

            return "Bandcamp", releases

        except Exception as e:
            raise ScrapeError(f"Failed to parse Bandcamp search results: {str(e)}") from e
