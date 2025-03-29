import asyncio
import re
from itertools import chain

from salmon.search.base import (
    ArtistRlsData,
    IdentData,
    LabelRlsData,
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
                IdentData(
                    rls["artist"]["name"], rls["title"], None, rls["nb_tracks"], "WEB"
                ),
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

    async def get_artist_releases(self, artiststr):
        """
        Get the releases of an artist on Deezer. Find their artist page and request
        all their releases.
        """
        artist_ids = await self._get_artist_ids(artiststr)
        tasks = [
            self._get_artist_albums(artist_id, artiststr) for artist_id in artist_ids
        ]
        return "Deezer", list(chain.from_iterable(await asyncio.gather(*tasks)))

    async def _get_artist_ids(self, artiststr):
        resp = await self.get_json("/search/artist", params={"q": artiststr})
        return [a["id"] for a in resp["data"] if a["name"].lower() == artiststr.lower()]

    async def _get_artist_albums(self, artist_id, artist_name):
        resp = await self.get_json(f"/artist/{artist_id}/albums")
        return [
            ArtistRlsData(
                url=rls["link"],
                quality="LOSSLESS",  # Cannot determine.
                year=self._parse_year(rls["release_date"]),
                artist=artist_name,
                album=rls["title"],
                label="",
                explicit=rls["explicit_lyrics"],
            )
            for rls in resp["data"]
        ]

    async def get_label_releases(self, labelstr, maximum=0, year=None):
        """Gets all the albums released by a label up to a total number.
        Year filtering doesn't actually work."""
        if year:
            yearstr = "year='" + year + "'"
        else:
            yearstr = ""
        url_str = f"/search/album&q=label:'{labelstr}' {yearstr}/albums"
        resp = await self.get_json(url_str)
        albums = []
        i = 0
        while i < maximum or maximum == 0:
            print(i)
            i += 25
            for rls in resp["data"]:
                album = await self.get_json(f"/album/{rls['id']}")
                albums.append(
                    LabelRlsData(
                        url=rls['link'],
                        quality="LOSSLESS",  # Cannot determine.
                        year=str(self._parse_year(album["release_date"])),
                        artist=rls['artist']['name'],
                        album=rls["title"],
                        type=album['record_type'],
                        explicit=rls["explicit_lyrics"],
                    )
                )
                if maximum > 0 and len(albums) >= maximum:
                    return "Deezer", albums
            if "next" in resp.keys():
                resp = await self.get_json(url_str, params={"index": i})
            else:
                return "Deezer", albums
        return "Deezer", albums

    @staticmethod
    def _parse_year(date):
        try:
            return int(re.search(r"(\d{4})", date)[0])
        except (ValueError, IndexError, TypeError):
            return None
