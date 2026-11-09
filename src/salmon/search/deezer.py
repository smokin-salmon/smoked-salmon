from salmon.search.base import (
    IdentData,
    SearchMixin,
)
from salmon.sources import DeezerBase


class Searcher(DeezerBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        releases = {}
        resp = await self.get_json("/search/album", params={"q": searchstr})
        # print(resp)
        for rls in resp["data"]:
            releases[rls["id"]] = (
                IdentData(rls["artist"]["name"], rls["title"], None, rls["nb_tracks"], "WEB"),
                self.format_result(
                    rls["artist"]["name"],
                    rls["title"],
                    None,
                    track_count=rls["nb_tracks"],
                ),
            )
            if len(releases) == limit:
                break
        return "Deezer", releases
