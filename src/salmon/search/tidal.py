import asyncio
import html
import re
from itertools import chain, zip_longest
from urllib.parse import quote

from salmon import cfg
from salmon.common import parse_copyright
from salmon.errors import ScrapeError
from salmon.search.base import ArtistRlsData, IdentData, SearchMixin
from salmon.sources import TidalBase
from salmon.sources.tidal import parse_quality

COUNTRIES = [cc.upper() for cc in cfg.metadata.tidal.regions]

SEARCH_INCLUDES = "albums,albums.artists,tracks,tracks.albums,tracks.albums.artists"


class Searcher(TidalBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        """
        Run a search of Tidal albums.
        Warnings are for stream quality/streambility.
        """
        if not (cfg.metadata.tidal.client_id and cfg.metadata.tidal.client_secret):
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

    async def _search_releases_country(self, searchstr, country_code, limit):
        """
        A separate coroutine for running a country-specific search. This is
        so we can run searches on all countries simultaneously from the primary
        search function.
        """
        resp = await self.get_json(
            f"/searchResults/{quote(searchstr)}",
            params={
                "countryCode": country_code,
                "include": SEARCH_INCLUDES,
            },
        )
        album_map = {obj["id"]: obj for obj in resp["included"] if obj["type"] == "albums"}

        albums = [
            album_map[rls["id"]] for rls in resp["data"]["relationships"]["albums"]["data"] if rls["id"] in album_map
        ][: limit * 2]  # Double it up to accomodate dupe results.

        single_ids = []
        for track in resp["data"]["relationships"]["tracks"]["data"]:
            track_obj = next(
                (obj for obj in resp["included"] if obj["type"] == "tracks" and obj["id"] == track["id"]),
                None,
            )
            if not track_obj:
                continue
            album_ids = [rel["id"] for rel in track_obj.get("relationships", {}).get("albums", {}).get("data", [])]
            for album_id in album_ids:
                if album_id in album_map and album_id not in single_ids:
                    single_ids.append(album_id)
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
            artists = html.unescape(", ".join(a["name"] for a in self._album_artists(rls, resp["included"])))
            title = attributes["title"]
            track_count = attributes["numberOfItems"]
            year_match = re.search(r"(\d{4})", attributes["releaseDate"]) if attributes["releaseDate"] else None
            year = year_match[1] if year_match else None
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

    @staticmethod
    def _album_artists(album: dict, included: list[dict]) -> list[dict]:
        """Return the artist resource objects of an album resource."""
        artist_ids = [rel["id"] for rel in album.get("relationships", {}).get("artists", {}).get("data", [])]
        by_id = {obj["id"]: obj for obj in included if obj["type"] == "artists"}
        return [
            {"id": artist_id, "name": by_id[artist_id]["attributes"]["name"]}
            for artist_id in artist_ids
            if artist_id in by_id
        ]

    async def get_artist_releases(self, artiststr):
        """
        Get the releases of an artist on Tidal. Find their artist page and request their
        Albums and EPs/Singles.
        """
        artist_ids = await self.get_artist_ids(artiststr)
        tasks = []
        for artist_id in artist_ids:
            for cc in COUNTRIES:
                tasks += [
                    self._get_artist_albums(artist_id, cc),
                    self._get_artist_eps_and_singles(artist_id, cc),
                ]
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
        resp = await self.get_json(
            f"/searchResults/{quote(artiststr)}",
            params={
                "countryCode": country_code,
                "include": "artists",
            },
        )
        return {
            obj["id"]
            for obj in resp["included"]
            if obj["type"] == "artists" and obj["attributes"]["name"].lower() == artiststr.lower()
        }

    async def _get_artist_albums(self, artist_id, country_code, album_types=None):
        """Fetch an artist's albums, optionally filtered by album type."""
        albums: list[tuple[dict, list[dict]]] = []
        cursor = None
        pages = 0
        try:
            while pages < 50:  # cap paging so a malformed nextCursor can't loop forever
                pages += 1
                params = {"countryCode": country_code, "include": "albums,albums.artists"}
                if cursor:
                    params["page[cursor]"] = cursor
                resp = await self.get_json(f"/artists/{artist_id}/relationships/albums", params=params)
                by_id = {obj["id"]: obj for obj in resp["included"] if obj["type"] == "albums"}
                albums += [(by_id[rls["id"]], resp["included"]) for rls in resp["data"] if rls["id"] in by_id]
                cursor = resp.get("links", {}).get("meta", {}).get("nextCursor")
                if not cursor:
                    break
        except ScrapeError:
            return []

        return [
            ArtistRlsData(
                url=self.format_url(rls_id=rls["id"]),
                quality=parse_quality(rls["attributes"].get("mediaTags", [])),
                year=self._parse_year(rls["attributes"].get("releaseDate")),
                artist=", ".join(a["name"] for a in self._album_artists(rls, included)),
                album=rls["attributes"]["title"],
                label=parse_copyright((rls["attributes"].get("copyright") or {}).get("text")),
                explicit=rls["attributes"]["explicit"],
            )
            for rls, included in albums
            if not album_types or rls["attributes"].get("albumType") in album_types
        ]

    async def _get_artist_eps_and_singles(self, artist_id, country_code):
        return await self._get_artist_albums(artist_id, country_code, album_types={"EP", "SINGLE"})

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
