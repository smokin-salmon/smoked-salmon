import re

from salmon.common import parse_copyright
from salmon.search.base import IdentData, SearchMixin
from salmon.sources import iTunesBase


class Searcher(iTunesBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        releases = {}
        resp = await self.get_json(
            "/search",
            params={
                "media": "music",
                "entity": "album",
                "limit": limit if limit < 25 else 25,
                "term": searchstr,
            },
        )
        results = resp["results"]
        for rls in results:
            artists = rls["artistName"]
            title = rls["collectionName"]
            track_count = rls["trackCount"]
            date = rls["releaseDate"][:10]
            year_match = re.search(r"(\d{4})", date)
            year = int(year_match[1]) if year_match else None
            copyright = parse_copyright(rls["copyright"]) if "copyright" in rls else None
            explicit = rls["collectionExplicitness"] == "explicit"
            clean = rls["collectionExplicitness"] == "cleaned"

            releases[rls["collectionId"]] = (
                IdentData(artists, title, year, track_count, "WEB"),
                self.format_result(
                    artists,
                    title,
                    f"{year or ''} {copyright or ''}".strip(),
                    track_count=track_count,
                    explicit=explicit,
                    clean=clean,
                ),
            )
        return "iTunes", releases
