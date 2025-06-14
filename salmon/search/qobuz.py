import re

from salmon import cfg
from salmon.errors import ScrapeError
from salmon.search.base import (
    IdentData,
    SearchMixin,
)
from salmon.sources.qobuz import QobuzBase


class Searcher(QobuzBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        if not cfg.metadata.qobuz:
            return "Qobuz", None

        releases = {}
        try:
            resp = await self.get_json(
                "/catalog/search",
                params={
                    "query": searchstr,
                    "limit": limit,
                    "offset": 0,
                    "facet": None
                },
                headers=self.headers
            )

            if not resp or "albums" not in resp or "items" not in resp["albums"]:
                return "Qobuz", {}

            for rls in resp["albums"]["items"]:
                try:
                    artists = rls["artist"]["name"]
                    title = rls["title"]
                    year = self._parse_year(rls.get("release_date_original"))
                    track_count = rls["tracks_count"]
                    
                    edition = f"{year}"
                    if rls.get("label", {}).get("name"):
                        edition += f" {rls['label']['name']}"
                    
                    format_details = []
                    if rls.get("hires"):
                        format_details.append("Hi-Res")
                    if rls.get("maximum_bit_depth"):
                        format_details.append(f"{rls['maximum_bit_depth']}bit")
                    
                    ed_title = ", ".join(format_details) if format_details else None

                    releases[rls["id"]] = (
                        IdentData(
                            artists,
                            title,
                            year,
                            track_count,
                            "WEB",
                        ),
                        self.format_result(
                            artists,
                            title,
                            edition,
                            track_count=track_count,
                            ed_title=ed_title,
                            explicit=rls.get("parental_warning", False)
                        ),
                    )
                except (KeyError, TypeError, AttributeError):
                    # Skip individual release if it has missing/malformed data
                    continue
                    
                if len(releases) == limit:
                    break
                    
            return "Qobuz", releases
        except Exception as e:
            raise ScrapeError(f"Failed to retrieve or parse Qobuz search results: {str(e)}") from e

    @staticmethod
    def _parse_year(date):
        try:
            return int(re.search(r"(\d{4})", date)[0])
        except (ValueError, IndexError, TypeError):
            return None

    @staticmethod
    def format_url(rls_id, rls_name=None):
        """Format a Qobuz URL from a release ID."""
        return f"https://www.qobuz.com/album/-/{rls_id}"
