import re

from salmon import cfg
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
        # soup can be BeautifulSoup or dict depending on source
        if not hasattr(soup, "find_all"):
            return "Junodownload", releases
        for meta in soup.find_all(  # type: ignore[union-attr]
            "div",
            attrs={
                "class": "row gutters-sm jd-listing-item",
                "data-ua_location": "release",
            },
        ):
            try:
                su_title = meta.find("a", attrs={"class": "juno-title"})
                if not su_title or not su_title.get("href"):
                    continue
                href_match = re.search(r"/products/[^/]+/([\d-]+)", str(su_title["href"]))
                if not href_match:
                    continue
                rls_id = href_match[1]
                title = su_title.string

                # right_blob = meta.find('div', attrs={'class': 'text-sm mb-3 mb-lg-3'})
                right_blob = meta.find("div", attrs={"class": "text-sm text-muted mt-3"})
                if not right_blob:
                    continue

                right_blob_elements_count = len(right_blob.get_text(separator="|").strip().split("|"))
                if right_blob_elements_count != 3:
                    # skip item missing one or more of: catno, date or genre
                    continue

                br_tag = right_blob.find("br")
                if not br_tag:
                    continue

                next_elem = br_tag.next
                if not next_elem or not hasattr(next_elem, "strip"):
                    continue
                date = str(next_elem).strip()
                year = int(date[-2:])

                year = 1900 + year if 40 <= year <= 99 else 2000 + year

                prev_sibling = br_tag.previous_sibling
                catno = str(prev_sibling).strip().replace(" ", "") if prev_sibling else ""

                ar_blob = meta.find("div", attrs={"class": "col juno-artist"})
                if not ar_blob:
                    continue

                ar_li = [a.string.title() for a in ar_blob.find_all("a") if a.string]
                artists = ", ".join(ar_li) if ar_li and len(ar_li) < 5 else cfg.upload.formatting.various_artist_word

                label_blob = meta.find("a", attrs={"class": "juno-label"})
                label = label_blob.text.strip() if label_blob else ""

                if label.lower() not in cfg.upload.search.excluded_labels:
                    releases[rls_id] = (
                        IdentData(artists, title, year, None, "WEB"),
                        self.format_result(artists, title, f"{year} {label} {catno}"),
                    )
            except (TypeError, IndexError, AttributeError) as e:
                raise ScrapeError("Failed to parse scraped search results.") from e
            if len(releases) == limit:
                break
        return "Junodownload", releases
