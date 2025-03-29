import re

from salmon.search.base import IdentData, SearchMixin
from salmon.sources import DiscogsBase

SOURCES = {
    "Vinyl": "Vinyl",
    "File": "WEB",
    "CD": "CD",
}


class Searcher(DiscogsBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        releases = {}
        resp = await self.get_json(
            "/database/search",
            params={"q": searchstr, "type": "release", "perpage": 50},
        )
        for rls in resp["results"]:
            artists, title = rls["title"].split(" - ", 1)
            year = rls.get("year", None)
            source = parse_source(rls["format"])
            ed_title = ", ".join(set(rls["format"]))

            edition = f"{year} {source}"
            if rls["label"] and rls["label"][0] != "Not On Label":
                edition += f" {rls['label'][0]} {rls['catno']}"
            else:
                edition += " Not On Label"

            releases[rls["id"]] = (
                IdentData(artists, title, year, None, source),
                self.format_result(artists, title, edition, ed_title=ed_title),
            )
            if len(releases) == limit:
                break
        return "Discogs", releases


def sanitize_artist_name(name):
    """
    Remove parenthentical number disambiguation bullshit from artist names,
    as well as the asterisk stuff.
    """
    name = re.sub(r" \(\d+\)$", "", name)
    return re.sub(r"\*+$", "", name)


def parse_source(formats):
    """
    Take the list of format strings provided by Discogs and iterate over them
    to find a possible source for the release.
    """
    for format_s, source in SOURCES.items():
        if any(format_s in f for f in formats):
            return source
