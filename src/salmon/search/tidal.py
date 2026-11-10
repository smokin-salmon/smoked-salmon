import asyncio
import html
import re
from itertools import zip_longest

from salmon import cfg
from salmon.common import parse_copyright
from salmon.errors import ScrapeError
from salmon.localization import text as _zh
from salmon.search.base import IdentData, SearchMixin, say_part_failed
from salmon.sources import TidalBase
from salmon.sources.tidal import credentials_configured

COUNTRIES = [cc.upper() for cc in cfg.metadata.tidal.regions]

SEARCH_INCLUDES = "albums,albums.artists,tracks,tracks.albums,tracks.albums.artists"


class Searcher(TidalBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        """
        Run a search of Tidal albums.
        Warnings are for stream quality/streambility.
        """
        if not credentials_configured():
            return "Tidal", {}

        releases, tasks = {}, []

        found_ids, identifiers = set(), set()
        for cc in COUNTRIES:
            tasks.append(self._search_releases_country(searchstr, cc, limit))
        answered = []
        for cc, answer in zip(COUNTRIES, await asyncio.gather(*tasks, return_exceptions=True), strict=True):
            if isinstance(answer, ScrapeError):
                say_part_failed(f"Tidal: region {cc}", answer)
            elif isinstance(answer, BaseException):
                raise answer
            else:
                answered.append(answer)
        if COUNTRIES and not answered:
            raise ScrapeError(_zh("Tidal: every region failed."))

        for rank in zip_longest(*answered):
            for rank_result in rank:
                if rank_result:
                    cc, rid, result = rank_result
                    if rid not in found_ids and result[1][5:] not in identifiers:
                        found_ids.add(rid)
                        identifiers.add(result[1][5:])
                        releases[(cc, rid)] = result
                if len(releases) == limit:
                    break
            if len(releases) == limit:
                break
        return "Tidal", releases

    async def _search(self, query: str, country_code: str, include: str) -> tuple[dict, list[dict]]:
        """Search Tidal and return the search results resource with the included resources."""
        resp = await self.get_json(
            "/searchResults",
            params={"filter[query]": query, "countryCode": country_code, "include": include},
        )
        # The document holds exactly one searchResults resource.
        results = resp["data"][0] if resp["data"] else {}
        return results, resp.get("included", [])

    async def _search_releases_country(self, searchstr, country_code, limit):
        """
        A separate coroutine for running a country-specific search. This is
        so we can run searches on all countries simultaneously from the primary
        search function.
        """
        search, included = await self._search(searchstr, country_code, SEARCH_INCLUDES)
        relationships = search.get("relationships", {})
        album_map = {obj["id"]: obj for obj in included if obj["type"] == "albums"}
        track_map = {obj["id"]: obj for obj in included if obj["type"] == "tracks"}

        albums = [
            album_map[rls["id"]] for rls in relationships.get("albums", {}).get("data", []) if rls["id"] in album_map
        ][: limit * 2]  # Double it up to accomodate dupe results.

        # Singles cannot be searched for as albums, so take the albums of the matching tracks.
        single_ids = []
        for track in relationships.get("tracks", {}).get("data", []):
            track_obj = track_map.get(track["id"])
            if not track_obj:
                continue
            for rel in track_obj.get("relationships", {}).get("albums", {}).get("data", []):
                if rel["id"] in album_map and rel["id"] not in single_ids:
                    single_ids.append(rel["id"])
            if len(single_ids) >= limit * 2 - len(albums) // 2:
                break
        singles = [album_map[rls_id] for rls_id in single_ids]

        results = []
        # Ghetto way of zipping into a list. SStaD!
        for alb, sgl in zip_longest(albums, singles):
            if alb:
                results.append(alb)
            if sgl:
                results.append(sgl)

        releases = []
        for rls in [r for r in results if r][: limit * 2]:
            attributes = rls["attributes"]
            artists = html.unescape(", ".join(a["name"] for a in self._parse_resource_artists(rls, included)))
            title = attributes["title"]
            track_count = attributes["numberOfItems"]
            year = self._parse_year(attributes.get("releaseDate"))
            copyright = parse_copyright((attributes.get("copyright") or {}).get("text"))
            explicit = attributes["explicit"]

            releases.append(
                (
                    country_code,
                    rls["id"],
                    (
                        IdentData(artists, title, year, track_count, "WEB"),
                        self.format_result(
                            artists,
                            title,
                            self.edition(year, copyright),
                            track_count=track_count,
                            country_code=country_code,
                            explicit=explicit,
                            clean=not explicit,
                        ),
                    ),
                )
            )
        return releases

    @staticmethod
    def _parse_year(date):
        try:
            match = re.search(r"(\d{4})", date)
            return int(match[0]) if match else None
        except (ValueError, IndexError, TypeError):
            return None
