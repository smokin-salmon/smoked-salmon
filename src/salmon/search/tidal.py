import asyncio
import html
import re
from itertools import chain, zip_longest

from salmon import cfg
from salmon.common import parse_copyright
from salmon.errors import ScrapeError
from salmon.search.base import ArtistRlsData, IdentData, SearchMixin
from salmon.sources import TidalBase
from salmon.sources.tidal import MAX_PAGES, credentials_configured, parse_quality

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
        for rank in zip_longest(*(await asyncio.gather(*tasks))):
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
                            f"{year} {copyright}",
                            track_count=track_count,
                            country_code=country_code,
                            explicit=explicit,
                            clean=not explicit,
                        ),
                    ),
                )
            )
        return releases

    async def get_artist_releases(self, artiststr):
        """
        Get the releases of an artist on Tidal: find their artist page and request
        all their releases (albums, EPs and singles).
        """
        artist_ids = await self.get_artist_ids(artiststr)
        tasks = [self._get_artist_albums(artist_id, cc) for artist_id in artist_ids for cc in COUNTRIES]
        return (
            "Tidal",
            self._filter_dupes(chain.from_iterable(await asyncio.gather(*tasks))),
        )

    async def get_artist_ids(self, artiststr):
        artist_ids = set()
        tasks = [self._search_artists_country(artiststr, cc) for cc in COUNTRIES]
        for artist_ids_new in await asyncio.gather(*tasks):
            artist_ids |= artist_ids_new
        return artist_ids

    async def _search_artists_country(self, artiststr, country_code):
        _, included = await self._search(artiststr, country_code, "artists")
        return {
            obj["id"]
            for obj in included
            if obj["type"] == "artists" and obj["attributes"]["name"].lower() == artiststr.lower()
        }

    async def _get_artist_albums(self, artist_id, country_code):
        """Fetch an artist's releases of every album type."""
        albums: list[dict] = []
        included: list[dict] = []
        cursor = None
        try:
            for _ in range(MAX_PAGES):
                params = {"countryCode": country_code, "include": "albums,albums.artists"}
                if cursor:
                    params["page[cursor]"] = cursor
                resp = await self.get_json(f"/artists/{artist_id}/relationships/albums", params=params)
                page_included = resp.get("included", [])
                by_id = {obj["id"]: obj for obj in page_included if obj["type"] == "albums"}
                albums += [by_id[rls["id"]] for rls in resp["data"] if rls["id"] in by_id]
                included += page_included
                cursor = self.next_cursor(resp.get("links", {}))
                if not cursor:
                    break
        except ScrapeError:
            return []

        return [
            ArtistRlsData(
                url=self.format_url(rls_id=rls["id"]),
                quality=parse_quality(rls["attributes"].get("mediaTags", [])),
                year=self._parse_year(rls["attributes"].get("releaseDate")),
                artist=", ".join(a["name"] for a in self._parse_resource_artists(rls, included)),
                album=rls["attributes"]["title"],
                label=parse_copyright((rls["attributes"].get("copyright") or {}).get("text")),
                explicit=rls["attributes"]["explicit"],
            )
            for rls in albums
        ]

    @staticmethod
    def _parse_year(date):
        try:
            match = re.search(r"(\d{4})", date)
            return int(match[0]) if match else None
        except (ValueError, IndexError, TypeError):
            return None

    @staticmethod
    def _filter_dupes(results):
        filtered = []
        existing_urls = set()
        for rls in results:
            if rls.url not in existing_urls:
                existing_urls.add(rls.url)
                filtered.append(rls)

        # Filter hi-res and lossless dupes.
        lossless = {rls.album for rls in filtered if rls.quality == "LOSSLESS"}
        for rls in [r for r in filtered if r.quality == "HI_RES"]:
            if rls.album in lossless:
                filtered.remove(rls)

        return sorted(filtered, key=lambda r: r.year, reverse=True)


def strip_parens(stri):
    return re.sub(r" [Ff]eat\..+| \(.+\)", "", stri).lower()
