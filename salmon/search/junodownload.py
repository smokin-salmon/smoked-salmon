import re

from salmon import config
from salmon.errors import ScrapeError
from salmon.search.base import IdentData, SearchMixin
from salmon.sources import JunodownloadBase


class Searcher(JunodownloadBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        releases = {}
        soup = await self.create_soup(
            self.search_url,
            params={
                "submit-search": "SEARCH",
                "solrorder": "relevancy",
                "q[all][]": [searchstr],
            },
            follow_redirects=False,
        )
        for meta in soup.find_all(
            'div',
            attrs={
                'class': 'row gutters-sm jd-listing-item',
                'data-ua_location': 'release',
            },
        ):
            try:
                su_title = meta.find('a', attrs={'class': 'juno-title'})
                rls_id = re.search(r"/products/[^/]+/([\d-]+)", su_title["href"])[1]
                title = su_title.string

                #right_blob = meta.find('div', attrs={'class': 'text-sm mb-3 mb-lg-3'})
                right_blob = meta.find('div', attrs={'class': 'text-sm text-muted mt-3'})

                right_blob_elements_count = len(
                    right_blob.get_text(separator="|").strip().split("|")
                )
                if right_blob_elements_count != 3:
                    # skip item missing one or more of: catno, date or genre
                    continue

                date = right_blob.find('br').next.strip()
                year = int(date[-2:])

                if 40 <= year <= 99:
                    year = 1900 + year
                else:
                    year = 2000 + year

                catno = right_blob.find('br').previous_sibling.strip().replace(' ', '')

                ar_blob = meta.find('div', attrs={'class': 'col juno-artist'})

                ar_li = [a.string.title() for a in ar_blob.find_all('a') if a.string]
                artists = (
                    ", ".join(ar_li)
                    if ar_li and len(ar_li) < 5
                    else config.VARIOUS_ARTIST_WORD
                )

                label_blob = meta.find('a', attrs={'class': 'juno-label'})
                label = label_blob.text.strip()

                if label.lower() not in config.SEARCH_EXCLUDED_LABELS:
                    releases[rls_id] = (
                        IdentData(artists, title, year, None, "WEB"),
                        self.format_result(artists, title, f"{year} {label} {catno}"),
                    )
            except (TypeError, IndexError, AttributeError) as e:
                raise ScrapeError("Failed to parse scraped search results.") from e
            if len(releases) == limit:
                break
        return "Junodownload", releases
