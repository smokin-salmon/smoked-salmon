import asyncio
import html
import re
from itertools import chain, zip_longest

from salmon import cfg
from salmon.common import parse_copyright
from salmon.errors import ScrapeError
from salmon.search.base import ArtistRlsData, IdentData, SearchMixin
from salmon.sources import TidalBase

COUNTRIES = cfg.metadata.tidal.search_regions


class Searcher(TidalBase, SearchMixin):
    async def search_releases(self, searchstr, limit):
        """
        Run a search of Tidal albums.
        Warnings are for stream quality/streambility.
        """
        if not cfg.metadata.tidal.token:
            return "Tidal", None

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
        releases = []
        resp = await self.get_json(
            "/search",
            params={
                "types": "ALBUMS,TRACKS",
                "query": searchstr,
                "countrycode": country_code,
            },
        )
        albums = resp["albums"]["items"][: limit * 2]  # Double it up to accomodate dupe results.
        singles = await self._parse_singles(resp["tracks"]["items"], country_code, limit * 2 - len(albums) // 2)

        results = []
        # Ghetto way of zipping into a list. SStaD!
        for alb, sgl in zip_longest(albums, singles):
            if alb:
                results.append(alb)
            if sgl:
                results.append(sgl)

        for rls in [r for r in results if r][: limit * 2]:
            artists = html.unescape(", ".join(a["name"] for a in rls["artists"] if a["type"] == "MAIN"))
            title = rls["title"]
            track_count = rls["numberOfTracks"]
            year_match = re.search(r"(\d{4})", rls["releaseDate"]) if rls["releaseDate"] else None
            year = year_match[1] if year_match else None
            copyright = parse_copyright(rls["copyright"])
            explicit = rls["explicit"]

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

    async def _parse_singles(self, track_results, country_code, limit):
        """
        Parse single track results from the tracks response. Single releases cannot
        be searched for as albums, which is a royal PITA. This is our way around that,
        at the cost of extra API calls.
        """
        # This has been turned into getting the albums of all matching tracks.
        singles = []
        for track in track_results:
            """
            if (abs(track['id'] - track['album']['id']) < 5
                    and track['trackNumber'] in {1, 2}
                    and track['volumeNumber'] == 1):
            """
            album = await self.get_json(f"/albums/{track['album']['id']}", params={"countryCode": country_code})
            # if album['numberOfTracks'] < 3:
            singles.append(album)
            if len(singles) == limit:
                break
        return singles

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
            "/search",
            params={
                "types": "ARTISTS",
                "query": artiststr,
                "countrycode": country_code,
            },
        )
        return {a["id"] for a in resp["artists"]["items"] if a["name"].lower() == artiststr.lower()}

    async def _get_artist_albums(self, artist_id, country_code):
        try:
            resp = await self.get_json(f"/artists/{artist_id}/albums", params={"countrycode": country_code})
        except ScrapeError:
            return []
        return [
            ArtistRlsData(
                url=rls["url"].replace("http://www.", "https://listen."),
                quality=rls["audioQuality"],
                year=self._parse_year(rls["releaseDate"]),
                artist=", ".join(a["name"] for a in rls["artists"] if a["type"] == "MAIN"),
                album=rls["title"],
                label=parse_copyright(rls["copyright"]),
                explicit=rls["explicit"],
            )
            for rls in resp["items"]
        ]

    async def _get_artist_eps_and_singles(self, artist_id, country_code):
        try:
            resp = await self.get_json(
                f"/artists/{artist_id}/albums",
                params={"countrycode": country_code, "filter": "EPSANDSINGLES"},
            )
        except ScrapeError:
            return []
        return [
            ArtistRlsData(
                url=rls["url"].replace("http://www.", "https://listen."),
                quality=rls["audioQuality"],
                year=self._parse_year(rls["releaseDate"]),
                artist=", ".join(a["name"] for a in rls["artists"] if a["type"] == "MAIN"),
                album=rls["title"],
                label=parse_copyright(rls["copyright"]),
                explicit=rls["explicit"],
            )
            for rls in resp["items"]
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
